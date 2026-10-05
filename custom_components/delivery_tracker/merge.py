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
from typing import Any, Final

from .const import (
    DEFAULT_SOURCE_PRIORITY,
    MAX_EVENTS_PER_SHIPMENT,
    MAX_ITEMS_PER_SHIPMENT,
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

    if incoming_status in REGRESSION_STATUSES:
        # Echte Rueckschritte gewinnen — es sei denn, der aktuelle Stand ist
        # ebenfalls ein Rueckschritt und stammt aus der besseren Quelle.
        if current_status in REGRESSION_STATUSES and not incoming_wins:
            return current_status, current.status_text
        return incoming_status, incoming.status_text

    if incoming_status.rank > current_status.rank:
        return incoming_status, incoming.status_text
    if incoming_status.rank == current_status.rank and incoming_wins:
        return incoming_status, incoming.status_text

    # Veralteter Status aus einer nachzuegelnden Quelle: verwerfen.
    return current_status, current.status_text


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
    """
    merged: dict[str, str] = {}
    for item in [*current, *incoming]:
        text = " ".join(item.split())
        key = text.casefold()
        if key and key not in merged:
            merged[key] = text
        if len(merged) >= limit:
            break
    return list(merged.values())


def merge_sources(current: list[SourceRecord], incoming: list[SourceRecord]) -> list[SourceRecord]:
    """Je Quelle den juengsten Nachweis behalten."""
    by_source: dict[str, SourceRecord] = {}
    for record in [*current, *incoming]:
        existing = by_source.get(record.source_id)
        if existing is None or record.fetched_at >= existing.fetched_at:
            by_source[record.source_id] = record
    return sorted(by_source.values(), key=lambda record: record.source_id)


def merge_shipments(current: Shipment, incoming: Shipment) -> Shipment:
    """Zwei Sichten auf dieselbe Sendung zu einem Objekt zusammenfuehren.

    `current` ist der gespeicherte Stand, `incoming` die neu eingetroffene Sicht.
    Das Ergebnis ist ein neues Objekt; die Eingaben bleiben unveraendert.
    """
    if current.id != incoming.id:
        raise ValueError(f"Sendungen mit verschiedener ID: {current.id} != {incoming.id}")

    wins = _incoming_wins(current, incoming)
    status, status_text = _merge_status(current, incoming, wins)

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
        # Jede Mail nennt nur ihre eigenen Empfaenger; zusammen ergeben sie das Bild.
        recipient_emails=merge_items(current.recipient_emails, incoming.recipient_emails),
        sender=_pick(current.sender, incoming.sender, wins),
        description=_pick(current.description, incoming.description, wins),
        items=merge_items(current.items, incoming.items),
        order_number=_pick(current.order_number, incoming.order_number, wins),
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
