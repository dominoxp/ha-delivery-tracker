"""Regelanwendung, die sich alle Carrier-Plugins teilen.

Hier steckt die gesamte Logik, die aus `rules.yaml` plus einer Mail eine Bewertung
und eine `Shipment` macht. Plugins mit Sonderfaellen erben davon und ueberschreiben
gezielt einzelne Schritte, statt alles neu zu schreiben.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Final

from homeassistant.util import dt as dt_util

from ..const import MAX_VALUE_LENGTH, SOURCE_IMAP
from ..dates import parse_eta
from ..mail import MailMessage
from ..merge import build_shipment_id
from ..models import Shipment, ShipmentEvent, ShipmentStatus, SourceRecord
from ..tracking import ChecksumResult, find_tracking_numbers, iter_candidates
from .rules import CarrierRules, StatusRule

# Gewichte der Trefferbewertung. Der Absender ist das staerkste Signal, ein
# blosser Trackingnummer-Fund das schwaechste — Nummernformate ueberschneiden sich
# zwischen Zustellern.
WEIGHT_FROM_DOMAIN: Final = 50
WEIGHT_SUBJECT: Final = 20
WEIGHT_TRACKING: Final = 15
WEIGHT_BODY: Final = 10

# Abzug fuer Massenversand ohne Trackingnummer. Zusteller verschicken unter
# derselben Absender-Domain auch Werbung; eine echte Versandbenachrichtigung
# traegt dagegen praktisch immer eine Nummer.
MALUS_BULK: Final = 25

_WHITESPACE: Final = re.compile(r"\s+")


def _clean(value: str | None) -> str | None:
    """Extrahierten Text saeubern: Whitespace normalisieren, Laenge deckeln."""
    if value is None:
        return None
    text = _WHITESPACE.sub(" ", value).strip()
    if not text:
        return None
    return text[:MAX_VALUE_LENGTH]


def _match_value(match: re.Match[str]) -> str | None:
    """Den interessanten Teil eines Treffers herausziehen.

    Konvention: benannte Gruppe `value`, sonst die erste Gruppe, sonst der
    gesamte Treffer.
    """
    if match.groupdict().get("value") is not None:
        return _clean(match.group("value"))
    if match.lastindex:
        return _clean(match.group(1))
    return _clean(match.group(0))


def first_match(patterns: Sequence[re.Pattern[str]], text: str) -> str | None:
    """Den ersten Treffer der Muster liefern; die Reihenfolge entscheidet."""
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None and (value := _match_value(match)) is not None:
            return value
    return None


def domain_matches(domain: str, allowed: Iterable[str]) -> bool:
    """True, wenn die Absender-Domain selbst oder eine ihrer Sub-Domains passt."""
    if not domain:
        return False
    return any(domain == entry or domain.endswith(f".{entry}") for entry in allowed)


def score_mail(rules: CarrierRules, mail: MailMessage) -> int:
    """Bewerten, wie gut eine Mail zu diesen Regeln passt.

    0 bedeutet „gehoert nicht zu mir". Bei mehreren Kandidaten gewinnt der
    hoechste Wert, deshalb ist die Bewertung additiv: Je mehr Signale
    zusammenkommen, desto sicherer der Treffer.

    Newsletter und Werbung desselben Absenders sind die haeufigste Fehlerquelle.
    Die Absender-Domain allein reicht deshalb nie — es muss immer ein
    inhaltliches Signal dazukommen.
    """
    text = mail.searchable_text
    if any(pattern.search(text) for pattern in rules.newsletter_patterns):
        return 0

    subject_hit = any(pattern.search(mail.subject) for pattern in rules.subject_patterns)
    tracking_hit = any(pattern.search(text) for pattern in rules.tracking_patterns)
    body_hit = any(pattern.search(text) for pattern in rules.body_patterns)
    if not (subject_hit or tracking_hit or body_hit):
        return 0

    score = WEIGHT_SUBJECT * subject_hit + WEIGHT_TRACKING * tracking_hit + WEIGHT_BODY * body_hit
    if rules.from_domains and domain_matches(mail.from_domain, rules.from_domains):
        score += WEIGHT_FROM_DOMAIN
    if mail.is_bulk and not tracking_hit:
        score -= MALUS_BULK

    # Der Bonus verschiebt Plugins gegeneinander (negativ z.B. beim generischen
    # Fallback), darf einen Treffer aber nie in ein Nicht-Treffen kippen.
    return max(1, score + rules.score_bonus)


def map_status(text: str, status_map: Sequence[StatusRule]) -> tuple[ShipmentStatus, str | None]:
    """Den Mailtext auf einen normalisierten Status abbilden.

    Ausgewertet wird in der Reihenfolge der `rules.yaml` — die erste passende
    Regel gewinnt. Spezifische Regeln gehoeren deshalb nach oben.
    """
    for rule in status_map:
        for pattern in rule.patterns:
            match = pattern.search(text)
            if match is not None:
                return rule.status, _clean(match.group(0))
    return ShipmentStatus.UNKNOWN, None


def extract_fields(rules: CarrierRules, text: str) -> dict[str, str | None]:
    """Alle deklarativ beschriebenen Felder aus dem Text ziehen."""
    return {name: first_match(patterns, text) for name, patterns in rules.fields.items()}


def extract_tracking_numbers(rules: CarrierRules, mail: MailMessage) -> list[str]:
    """Alle Trackingnummern einer Mail — normalisiert, gefiltert, ohne Dubletten."""
    return find_tracking_numbers(
        mail.searchable_text,
        rules.tracking_patterns,
        schemes=rules.checksum,
        require_context=rules.require_context,
    )


def check_tracking_number(rules: CarrierRules, number: str) -> ChecksumResult | None:
    """Pruefen, ob eine einzelne, normalisierte Nummer zu diesen Regeln passt.

    Dieselben Muster und Pruefverfahren wie bei der Mailauswertung, nur ohne
    Kontextpruefung — eine von Hand eingegebene Nummer hat kein Umfeld.
    `None` heisst: Kein Muster erfasst die Nummer vollstaendig.
    """
    for candidate in iter_candidates(number, rules.tracking_patterns, schemes=rules.checksum):
        if candidate.number == number:
            return candidate.checksum
    return None


def build_shipments_from_rules(
    rules: CarrierRules,
    mail: MailMessage,
    *,
    source_id: str = SOURCE_IMAP,
    now: datetime | None = None,
    tracking_numbers: Sequence[str] | None = None,
) -> list[Shipment]:
    """Aus Regeln und Mail alle enthaltenen Sendungen bauen.

    `tracking_numbers` ersetzt die Nummernsuche der Regeln — fuer Plugins, die
    strenger auswaehlen, als es die Regeln ausdruecken koennen (`generic`).

    Eine Sammelbestaetigung nennt mehrere Trackingnummern; jede davon ist eine
    eigene Sendung. Status, Liefertermin und Kontextfelder gelten dabei fuer alle
    gleichermassen — feiner aufloesen kann nur ein Plugin, das das Layout seiner
    Mails kennt (Step 1.3).

    Eine leere Liste heisst: Die Mail enthaelt weder Tracking- noch Bestellnummer
    und ist damit keiner Sendung zuzuordnen.
    """
    fetched_at = now or dt_util.utcnow()
    observed_at = mail.date or fetched_at
    text = mail.searchable_text

    numbers = (
        list(tracking_numbers)
        if tracking_numbers is not None
        else extract_tracking_numbers(rules, mail)
    )
    values = extract_fields(rules, text)
    order_number = values.get("order_number")
    if not numbers and not order_number:
        return []

    status, status_text = map_status(text, rules.status_map)

    eta_start, eta_end = None, None
    if (eta_text := first_match(rules.eta_patterns, text)) is not None:
        eta_start, eta_end = parse_eta(eta_text, now=observed_at)

    delivered_at = observed_at if status is ShipmentStatus.DELIVERED else None
    if (delivered_text := first_match(rules.delivered_patterns, text)) is not None:
        delivered_start, _ = parse_eta(delivered_text, now=observed_at)
        delivered_at = delivered_start or delivered_at

    shipments: list[Shipment] = []
    # Ohne Trackingnummer bleibt die Bestellnummer als Anker — die spaetere
    # Versandmail rastet ueber dieselbe ID darauf ein.
    for tracking_number in numbers or [None]:
        try:
            shipment_id = build_shipment_id(
                rules.carrier_id,
                tracking_number,
                sender=values.get("sender"),
                order_number=order_number,
            )
        except ValueError:
            continue

        events: list[ShipmentEvent] = []
        if status is not ShipmentStatus.UNKNOWN:
            events.append(ShipmentEvent(timestamp=observed_at, status=status, text=status_text))

        shipments.append(
            Shipment(
                id=shipment_id,
                carrier=rules.carrier_id,
                tracking_number=tracking_number,
                status=status,
                status_text=status_text,
                eta_start=eta_start,
                eta_end=eta_end,
                delivered_at=delivered_at,
                destination_raw=values.get("destination_raw"),
                sender=values.get("sender"),
                description=values.get("description"),
                order_number=order_number,
                pickup_location=values.get("pickup_location"),
                pickup_code=values.get("pickup_code"),
                tracking_url=rules.tracking_url(tracking_number),
                events=events,
                sources=[
                    SourceRecord(
                        source_id=source_id,
                        fetched_at=fetched_at,
                        reference=mail.message_id or None,
                        subject=mail.subject or None,
                        sender=mail.sender,
                    )
                ],
                first_seen=observed_at,
                last_updated=observed_at,
            )
        )
    return shipments


def build_shipment_from_rules(
    rules: CarrierRules,
    mail: MailMessage,
    *,
    source_id: str = SOURCE_IMAP,
    now: datetime | None = None,
) -> Shipment | None:
    """Die erste Sendung einer Mail — fuer Aufrufer, die nur eine erwarten."""
    shipments = build_shipments_from_rules(rules, mail, source_id=source_id, now=now)
    return shipments[0] if shipments else None
