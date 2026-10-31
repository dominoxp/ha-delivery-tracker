"""Bildung der Sendungs-ID und Zusammenfuehren von Sendungen aus mehreren Quellen.

Grundregeln (siehe `planning.md`):
- Schluessel einer Sendung ist `{carrier}_{normalisierte_trackingnummer}`.
- Der Status faellt nie zurueck; Ausnahme sind fachlich echte Rueckschritte
  (`delayed`, `delivery_failed`, `returned`).
- Kontextfelder, die nur eine Quelle kennt, werden nie mit `None` ueberschrieben.
- Bei echten Konflikten gewinnt die hoeherwertige Quelle, bei Gleichstand die frischere.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Final

from .const import (
    DEFAULT_SOURCE_PRIORITY,
    MAX_EVENTS_PER_SHIPMENT,
    MAX_ITEMS_PER_SHIPMENT,
    MAX_VALUE_LENGTH,
    SOURCE_PRIORITIES,
)
from .models import (
    REGRESSION_STATUSES,
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
    SourceRecord,
)
from .tracking import normalize_tracking_number

_NON_ALNUM: Final = re.compile(r"[^a-z0-9]+")

# Obergrenze der Herkunftsnachweise je Sendung (`merge_sources`).
MAX_SOURCE_RECORDS: Final = 20


def tracking_id_token(value: str) -> str:
    """Die Trackingnummer auf den Teil reduzieren, der in die Sendungs-ID geht.

    `normalize_tracking_number` raeumt bereits Trennzeichen weg; hier fallen
    zusaetzlich Sonderzeichen und Grossschreibung weg, damit die ID ein
    unauffaelliger Bezeichner bleibt.
    """
    return _NON_ALNUM.sub("", normalize_tracking_number(value).lower())


def _slug(value: str | None) -> str:
    """Beliebigen Text in einen ID-tauglichen Bestandteil ueberfuehren."""
    if not value:
        return ""
    return _NON_ALNUM.sub("_", value.lower()).strip("_")


def build_shipment_id(
    carrier: str,
    tracking_number: str | None = None,
    *,
    sender: str | None = None,
    order_number: str | None = None,
    package: str | None = None,
) -> str:
    """Die stabile ID einer Sendung bilden.

    Ohne Trackingnummer (reine Bestellbestaetigung) wird sie aus Haendler und
    Bestellnummer abgeleitet, damit die Bestellung ueberhaupt einen Anker hat.

    `package` unterscheidet mehrere Pakete derselben Bestellung. Amazon zum
    Beispiel vergibt gar keine Trackingnummern mehr, sondern nur eine
    `shipmentId` je Paket — ohne diesen Zusatz wuerden die vier Pakete einer
    Bestellung zu einer einzigen Sendung verschmelzen.

    Achtung: Die spaetere Versandmail bekommt dann eine *andere*, nummernbasierte
    ID und rastet **nicht** von selbst auf die Bestellung ein — `merge_shipments`
    setzt gleiche IDs voraus. Das Zusammenfuehren zweier getrennt gefuehrter
    Sendungen anhand der Bestellnummer ist in Step 3.2 vorgesehen.
    """
    carrier_slug = _slug(carrier) or "unknown"

    if tracking_number:
        token = tracking_id_token(tracking_number)
        if token:
            return f"{carrier_slug}_{token}"

    if order_number:
        parts = [carrier_slug, "order", _slug(sender), _slug(order_number), _slug(package)]
        return "_".join(part for part in parts if part)

    if package:
        return f"{carrier_slug}_{_slug(package)}"

    raise ValueError("Sendungs-ID braucht eine Trackingnummer oder eine Bestellnummer")


def source_priority(source_id: str) -> int:
    """Wertigkeit einer Quelle; unbekannte Quellen liegen unten.

    Die feinere Priorisierung samt Konfliktaufloesung folgt in Step 3.2.
    """
    return SOURCE_PRIORITIES.get(source_id, DEFAULT_SOURCE_PRIORITY)


def _shipment_priority(shipment: Shipment) -> int:
    """Hoechste Quellen-Wertigkeit, die zu dieser Sendung beigetragen hat."""
    if not shipment.sources:
        return DEFAULT_SOURCE_PRIORITY
    return max(source_priority(record.source_id) for record in shipment.sources)


def _incoming_wins(current: Shipment, incoming: Shipment) -> bool:
    """Entscheidet echte Feldkonflikte: hoehere Quelle, bei Gleichstand frischer."""
    return (_shipment_priority(incoming), incoming.last_updated) >= (
        _shipment_priority(current),
        current.last_updated,
    )


def _is_empty(value: Any) -> bool:
    """Leere Strings zaehlen wie `None` — Parser liefern gern beides."""
    return value is None or (isinstance(value, str) and not value.strip())


def _pick[T](current: T | None, incoming: T | None, incoming_wins: bool) -> T | None:
    """Ein Feld waehlen: Wissen geht vor Frische, niemals mit Leere ueberschreiben."""
    if _is_empty(incoming):
        return current
    if _is_empty(current):
        return incoming
    return incoming if incoming_wins else current


def _merge_status(
    current: Shipment, incoming: Shipment, incoming_wins: bool
) -> tuple[ShipmentStatus, str | None]:
    """Status und zugehoerigen Originaltext bestimmen."""
    current_status = current.status
    incoming_status = incoming.status

    if incoming_status is ShipmentStatus.UNKNOWN:
        return current_status, current.status_text
    if current_status is ShipmentStatus.UNKNOWN:
        return incoming_status, incoming.status_text

    if _is_replay(current, incoming):
        # Dieselbe Beobachtung noch einmal, etwa beim Neu-Einlesen des
        # Postfachs: Sie ist laengst eingerechnet und darf einen spaeteren
        # Stand nicht zuruecksetzen — auch nicht als Rueckschritt.
        return current_status, current.status_text

    if incoming_status in REGRESSION_STATUSES:
        # Echte Rueckschritte gewinnen — es sei denn, der aktuelle Stand ist
        # ebenfalls ein Rueckschritt und stammt aus der besseren Quelle.
        if current_status in REGRESSION_STATUSES and not incoming_wins:
            return current_status, current.status_text
        return incoming_status, incoming.status_text

    if (
        current_status in REGRESSION_STATUSES
        and incoming_status.rank >= ShipmentStatus.OUT_FOR_DELIVERY.rank
        and incoming.last_updated > current.last_updated
    ):
        # Nach einem Rueckschritt geht es weiter: Auf „nicht zugestellt" folgt
        # „liegt in der Filiale", auf „verspaetet" die Zustellung. Ein spaeter
        # Zwischenstand wie „unterwegs" bleibt dagegen verworfen.
        return incoming_status, incoming.status_text

    if incoming_status.rank > current_status.rank:
        return incoming_status, incoming.status_text
    if incoming_status.rank == current_status.rank and incoming_wins:
        return incoming_status, incoming.status_text

    # Veralteter Status aus einer nachzuegelnden Quelle: verwerfen.
    return current_status, current.status_text


def _is_replay(current: Shipment, incoming: Shipment) -> bool:
    """True, wenn jedes Ereignis von `incoming` schon in `current` steht."""
    if not incoming.events:
        return False
    known = {(event.timestamp, event.status) for event in current.events}
    return all((event.timestamp, event.status) in known for event in incoming.events)


def merge_events(
    current: list[ShipmentEvent],
    incoming: list[ShipmentEvent],
    *,
    limit: int = MAX_EVENTS_PER_SHIPMENT,
) -> list[ShipmentEvent]:
    """Event-Listen vereinigen, deduplizieren, sortieren und deckeln.

    Dubletten sind Events mit gleichem Zeitpunkt und Status; von diesen bleibt
    das ausfuehrlichere erhalten. Gedeckelt wird auf die juengsten `limit` Events.
    """
    by_key: dict[tuple[Any, ...], ShipmentEvent] = {}
    for event in [*current, *incoming]:
        existing = by_key.get(event.dedup_key)
        if existing is None or event.detail_score > existing.detail_score:
            by_key[event.dedup_key] = event

    merged = sorted(by_key.values(), key=lambda event: event.timestamp)
    if len(merged) > limit:
        merged = merged[-limit:]
    return merged


def merge_items(
    current: list[str],
    incoming: list[str],
    *,
    limit: int = MAX_ITEMS_PER_SHIPMENT,
) -> list[str]:
    """Artikellisten vereinigen statt ersetzen.

    Keine Quelle nennt bewusst *weniger* Artikel — eine kuerzere Liste ist
    Informationsmangel, kein Widerspruch. Amazon zaehlt dieselben Artikel in
    Versand- und Abholmail zudem in verschiedener Reihenfolge auf, und die
    Zustellmail nennt gar keinen. Die erste Nennung bestimmt die Reihenfolge.

    Gekuerzte Namen („Testartikel Sens...") gelten als derselbe Artikel wie
    jede Fassung, die damit beginnt — AliExpress schneidet denselben Titel je
    Mail an anderer Stelle ab. Die laengere Fassung nimmt den Platz der
    kuerzeren ein.
    """
    merged: list[str] = []
    for item in [*current, *incoming]:
        text = " ".join(item.split())
        if not text:
            continue
        index = next((index for index, known in enumerate(merged) if _same_item(known, text)), None)
        if index is None:
            if len(merged) >= limit:
                break
            merged.append(text)
        elif len(_stem(text)) > len(_stem(merged[index])):
            merged[index] = text
    return merged


def describe_items(titles: list[str]) -> str | None:
    """Mehrere Artikel eines Pakets zu einem Anzeigetext zusammenfassen."""
    if not titles:
        return None
    if len(titles) == 1:
        return titles[0]
    weitere = len(titles) - 1
    suffix = f" (+{weitere} weiterer Artikel)" if weitere == 1 else f" (+{weitere} weitere Artikel)"
    return f"{titles[0][: MAX_VALUE_LENGTH - len(suffix)]}{suffix}"


def _merge_description(
    current: Shipment, incoming: Shipment, items: list[str], wins: bool
) -> str | None:
    """Den Anzeigetext waehlen — und neu bilden, wenn er nur aus den Artikeln stammt.

    Nennt jede Mail einen anderen Artikel, waere sonst nur einer davon zu
    sehen. Eine Beschreibung, die der Zusteller selbst liefert („Artikel: …"),
    bleibt dagegen unangetastet.
    """
    picked = _pick(current.description, incoming.description, wins)
    generated = {describe_items(current.items), describe_items(incoming.items)} - {None}
    if items and picked in generated:
        return describe_items(items)
    return picked


def _stem(text: str) -> str:
    """Der Name ohne Auslassungszeichen am Ende, kleingeschrieben."""
    key = text.casefold()
    for ellipsis in ("...", "…"):
        if key.endswith(ellipsis):
            return key[: -len(ellipsis)].rstrip()
    return key


def _same_item(first: str, second: str) -> bool:
    """True, wenn beide Namen denselben Artikel meinen — gekuerzt oder nicht."""
    a, b = first.casefold(), second.casefold()
    if a == b:
        return True
    a_cut, b_cut = _stem(first) != a, _stem(second) != b
    return (a_cut and b.startswith(_stem(first))) or (b_cut and a.startswith(_stem(second)))


def merge_sources(current: list[SourceRecord], incoming: list[SourceRecord]) -> list[SourceRecord]:
    """Je Quelle und Rohdatum (Mail) den juengsten Nachweis behalten, aelteste zuerst.

    Jede Mail bleibt einzeln stehen, damit die Sendungsverwaltung zeigt, aus
    welchen Mails eine Sendung entstanden ist. API-Abrufe ohne Referenz fallen
    auf einen Eintrag je Quelle zusammen. Die Liste ist gedeckelt; es bleiben
    die juengsten Nachweise.
    """
    by_key: dict[tuple[str, str | None], SourceRecord] = {}
    for record in [*current, *incoming]:
        key = (record.source_id, record.reference)
        existing = by_key.get(key)
        if existing is None or record.fetched_at >= existing.fetched_at:
            by_key[key] = record
    ordered = sorted(by_key.values(), key=lambda record: (record.fetched_at, record.source_id))
    return ordered[-MAX_SOURCE_RECORDS:]


def merge_shipments(current: Shipment, incoming: Shipment) -> Shipment:
    """Zwei Sichten auf dieselbe Sendung zu einem Objekt zusammenfuehren.

    `current` ist der gespeicherte Stand, `incoming` die neu eingetroffene Sicht.
    Das Ergebnis ist ein neues Objekt; die Eingaben bleiben unveraendert.
    """
    if current.id != incoming.id:
        raise ValueError(f"Sendungen mit verschiedener ID: {current.id} != {incoming.id}")

    wins = _incoming_wins(current, incoming)
    status, status_text = _merge_status(current, incoming, wins)
    items = merge_items(current.items, incoming.items)

    delivered_at = _pick(current.delivered_at, incoming.delivered_at, wins)
    if status is not ShipmentStatus.DELIVERED:
        # Ein Rueckschritt (Retoure, gescheiterte Zustellung) hebt eine frueher
        # gemeldete Zustellung wieder auf.
        delivered_at = None

    return Shipment(
        id=current.id,
        carrier=_pick(current.carrier, incoming.carrier, wins) or current.carrier,
        tracking_number=_pick(current.tracking_number, incoming.tracking_number, wins),
        status=status,
        status_text=status_text,
        eta_start=_pick(current.eta_start, incoming.eta_start, wins),
        eta_end=_pick(current.eta_end, incoming.eta_end, wins),
        delivered_at=delivered_at,
        destination_id=_pick(current.destination_id, incoming.destination_id, wins),
        destination_raw=_pick(current.destination_raw, incoming.destination_raw, wins),
        # Eine Festlegung des Nutzers kommt nur von ihm selbst und bleibt stehen.
        destination_override=incoming.destination_override or current.destination_override,
        # Jede Mail nennt nur ihre eigenen Empfaenger; zusammen ergeben sie das Bild.
        recipient_emails=merge_items(current.recipient_emails, incoming.recipient_emails),
        sender=_pick(current.sender, incoming.sender, wins),
        description=_merge_description(current, incoming, items, wins),
        items=items,
        order_number=_pick(current.order_number, incoming.order_number, wins),
        order_numbers=merge_items(current.order_numbers, incoming.order_numbers),
        pickup_location=_pick(current.pickup_location, incoming.pickup_location, wins),
        pickup_code=_pick(current.pickup_code, incoming.pickup_code, wins),
        tracking_url=_pick(current.tracking_url, incoming.tracking_url, wins),
        events=merge_events(current.events, incoming.events),
        sources=merge_sources(current.sources, incoming.sources),
        first_seen=min(current.first_seen, incoming.first_seen),
        last_updated=max(current.last_updated, incoming.last_updated),
    )


def has_status_changed(previous: Shipment | None, current: Shipment) -> bool:
    """True, wenn sich der Status geaendert hat — Grundlage der HA-Events in 2.5."""
    if previous is None:
        return current.status is not ShipmentStatus.UNKNOWN
    return previous.status is not current.status


# ----------------------------------------------------------------------
# Getrennt gefuehrte Sendungen derselben Bestellung zusammenfuehren
# ----------------------------------------------------------------------


def package_anchor(shipment: Shipment) -> str | None:
    """Die Paketkennung in der ID (`shipmentId`/`pkgId`), sonst `None`.

    Eine reine Bestellnummer oder ein Blockindex der Bestellbestaetigung
    („…_0") ist keine Kennung: Beides sagt nicht, um welches Paket es geht.
    """
    if not shipment.order_number:
        return None
    marker = f"_{_slug(shipment.order_number)}"
    position = shipment.id.find(marker)
    if position < 0:
        return None
    token = shipment.id[position + len(marker) :].strip("_")
    return token if token and not token.isdigit() else None


def _item_set(shipment: Shipment) -> frozenset[str]:
    """Die Artikel als Menge — Amazon zaehlt sie je Mail in anderer Reihenfolge auf."""
    return frozenset(" ".join(item.casefold().split()) for item in shipment.items if item.strip())


def merge_target(shipment: Shipment, candidates: Iterable[Shipment]) -> Shipment | None:
    """Die Sendung, in die `shipment` gehoert — oder `None`, wenn das nicht eindeutig ist.

    Kriterium (Step 3.2): gleiche Bestellnummer **und** die Artikel von
    `shipment` sind Teil der Artikel des Ziels. Die Bestellnummer allein reicht
    nie, weil alle Pakete einer Bestellung sie teilen. Zwei Sendungen mit
    verschiedenen Paketkennungen sind immer verschiedene Pakete, auch bei
    gleichem Artikel. Ohne Artikel gibt es kein Kriterium — dann bleibt es
    bei der getrennten Sendung.

    Passen mehrere Ziele, die untereinander dieselbe Sendung sind (gleiche
    Artikel, keine widerspruechlichen Kennungen), gewinnt die aelteste. Sonst
    ist die Zuordnung mehrdeutig und unterbleibt.
    """
    items = _item_set(shipment)
    if not shipment.order_number or not items:
        return None
    anchor = package_anchor(shipment)
    found = [
        candidate
        for candidate in candidates
        if candidate.id != shipment.id
        and candidate.carrier == shipment.carrier
        and candidate.order_number == shipment.order_number
        and items <= _item_set(candidate)
        and not (anchor and (other := package_anchor(candidate)) and other != anchor)
    ]
    if not found:
        return None
    if len(found) > 1:
        first = _item_set(found[0])
        anchors = {package_anchor(candidate) for candidate in found} - {None}
        if len(anchors) > 1 or any(_item_set(candidate) != first for candidate in found):
            return None
    return min(found, key=lambda candidate: (candidate.first_seen, candidate.id))


def absorb_target(shipment: Shipment, candidates: Iterable[Shipment]) -> Shipment | None:
    """Das spaetere Paket, in dem `shipment` aufgeht — oder `None`.

    Fuer Marktplaetze, die Bestellungen buendeln und unterwegs neue Nummern
    vergeben. Bei AliExpress schickt jeder Haendler seine Bestellung als
    Zubringer (`LP…`) ins Lager, dort entsteht ein Sammelpaket (`AP…`), und
    das bekommt zuletzt die Nummer von Hermes oder DHL. Keine Mail nennt zwei
    dieser Nummern; verbunden sind sie nur ueber die Bestellungen darin.

    Kriterium: derselbe Absender, und die Bestellungen von `shipment` stecken
    alle im Ziel. Ausserdem muss das Ziel eine spaetere Stufe sein:
    - `shipment` hat noch keine Trackingnummer (reine Bestellung), oder
    - `shipment` liegt noch beim Marktplatz, und das Ziel enthaelt echt mehr
      Bestellungen (Zubringer -> Sammelpaket) oder liegt schon bei einem
      Zusteller (Sammelpaket -> Hermes).
    Zwei Zubringer derselben Bestellung sind dagegen zwei Pakete und bleiben
    getrennt, bis beide im Sammelpaket aufgehen. Ein Paket bei Hermes oder
    DHL geht nie in einem anderen auf.

    Passen mehrere Ziele, gewinnt das, das selbst in keinem anderen aufgeht.
    Bleiben dann noch mehrere (eine Bestellung in zwei Paketen), geht nur eine
    reine Bestellung im aeltesten auf — sonst stuende sie ewig als „bestellt"
    in der Liste. Ein Paket mit Nummer bleibt in dem Fall lieber getrennt.
    """
    orders = set(shipment.order_numbers)
    if not orders or not shipment.sender:
        return None
    found = [
        candidate
        for candidate in candidates
        if candidate.id != shipment.id and _supersedes(candidate, shipment)
    ]
    if len(found) > 1:
        found = [
            candidate
            for candidate in found
            if not any(
                other.id != candidate.id and _supersedes(other, candidate) for other in found
            )
        ]
    if not found or (len(found) > 1 and shipment.tracking_number):
        return None
    return min(found, key=lambda candidate: (candidate.first_seen, candidate.id))


def _supersedes(candidate: Shipment, shipment: Shipment) -> bool:
    """True, wenn `candidate` eine spaetere Stufe des Pakets `shipment` ist."""
    orders = set(shipment.order_numbers)
    if not orders or candidate.sender != shipment.sender or not candidate.tracking_number:
        return False
    if not orders <= set(candidate.order_numbers):
        return False
    if not shipment.tracking_number:
        return True
    # Ein Paket bei Hermes oder DHL ist die letzte Stufe. Teilt es Bestellungen
    # mit einem anderen, ist das eine geteilte Lieferung — zwei Pakete.
    if not _at_marketplace(shipment):
        return False
    if len(set(candidate.order_numbers)) > len(orders):
        return True
    # Gleiche Bestellungen: Was noch beim Marktplatz liegt, geht in dem auf,
    # was schon bei Hermes oder DHL ist. Die Reihenfolge der Mails taugt dafuer
    # nicht — AliExpress meldet sich zum Sammelpaket auch noch, wenn DHL
    # laengst unterwegs ist.
    return not _at_marketplace(candidate)


def _at_marketplace(shipment: Shipment) -> bool:
    """True, solange die Sendung beim Haendler selbst gefuehrt wird.

    Marktplatz-Plugins setzen sich selbst als Absender; nach der Uebergabe an
    einen Zusteller (`CarrierRegistry`) weicht die Carrier-ID davon ab.
    """
    return _slug(shipment.sender) == shipment.carrier


def same_items(first: Shipment, second: Shipment) -> bool:
    """True, wenn beide Sendungen dieselben Artikel tragen."""
    return _item_set(first) == _item_set(second)
