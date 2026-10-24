"""Aufbereitung fuer die Sendungsverwaltung im Options-Flow.

Der Options-Flow zeigt Sendungen und das Mail-Protokoll als Auswahllisten und
Markdown-Beschreibungen. Was dort steht, entsteht hier: Zeilen der Auswahlliste,
die Detailansicht einer Sendung und die Liste der letzten Mails.

Die Texte kommen aus den Uebersetzungen (`selector.manage_text`,
`selector.shipment_status`, `selector.mail_outcome`), damit die Verwaltung in
derselben Sprache erscheint wie der Rest der Oberflaeche. `selector` ist dabei
nur der Ort, an dem HA frei benannte Texte einer Integration zulaesst.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import DESTINATION_UNKNOWN, DESTINATION_UNKNOWN_NAME, DOMAIN
from .models import Shipment
from .pipeline import MailLogEntry, MailOutcome
from .store import Expiry, ExpiryRule

# HTML-Reste, die ein Parser aus der Mail mitgenommen hat („Paketshop 785</a><br />").
_TAG: Final = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
# Zeichen, die in einer Markdown-Zeile oder Tabelle etwas bedeuten.
_MARKDOWN: Final = re.compile(r"([\\`*_\[\]<>|#])")
# Obergrenze fuer Betreffs und Absender in Tabellen.
MAX_CELL_LENGTH: Final = 90

TEXT_KEYS: Final = "manage_text"
# Platzhalter fuer fehlende Werte.
EMPTY: Final = "-"


@dataclass(frozen=True, slots=True)
class Texts:
    """Die uebersetzten Texte der Verwaltung, mit Platzhaltern wie `{days}`."""

    strings: Mapping[str, str]

    def get(self, key: str, **values: object) -> str:
        """Ein Text aus `selector.manage_text`, Platzhalter ersetzt."""
        text = self.strings.get(f"{TEXT_KEYS}.{key}", key)
        return text.format(**values) if values else text

    def status(self, value: str) -> str:
        """Anzeigename eines `ShipmentStatus`."""
        return self.strings.get(f"shipment_status.{value}", value)

    def outcome(self, value: MailOutcome) -> str:
        """Anzeigename eines Mail-Ergebnisses."""
        return self.strings.get(f"mail_outcome.{value.value}", value.value)


async def async_load_texts(hass: HomeAssistant) -> Texts:
    """Texte in der Sprache der Instanz laden (Fallback Englisch macht HA)."""
    translations = await async_get_translations(hass, hass.config.language, "selector", {DOMAIN})
    prefix = f"component.{DOMAIN}.selector."
    strings: dict[str, str] = {}
    for key, text in translations.items():
        if key.startswith(prefix):
            # „manage_text.options.today" -> „manage_text.today"
            group, _, rest = key[len(prefix) :].partition(".options.")
            if rest:
                strings[f"{group}.{rest}"] = text
    return Texts(strings)


def md(value: object, limit: int | None = None) -> str:
    """Freitext so maskieren, dass er Markdown und Tabellen nicht zerlegt."""
    text = " ".join(_TAG.sub(" ", str(value)).split())
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return _MARKDOWN.sub(r"\\\1", text)


def format_dt(value: datetime | None) -> str:
    """Zeitpunkt in Ortszeit, kurz und sprachneutral."""
    if value is None:
        return EMPTY
    return dt_util.as_local(value).strftime("%d.%m.%Y %H:%M")


def format_date(value: datetime | None) -> str:
    """Datum in Ortszeit."""
    if value is None:
        return EMPTY
    return dt_util.as_local(value).strftime("%d.%m.%Y")


def _days_between(earlier: datetime, later: datetime) -> int:
    """Kalendertage zwischen zwei Zeitpunkten in Ortszeit."""
    return (dt_util.as_local(later).date() - dt_util.as_local(earlier).date()).days


def age_text(texts: Texts, value: datetime, now: datetime) -> str:
    """„heute", „gestern" oder „vor 37 Tagen"."""
    days = _days_between(value, now)
    if days <= 0:
        return texts.get("today")
    if days == 1:
        return texts.get("yesterday")
    return texts.get("days_ago", days=days)


def expiry_short(texts: Texts, expiry: Expiry, now: datetime) -> str:
    """„verschwindet in 23 Tagen" fuer die Listenzeile."""
    if expiry.at is None:
        return texts.get("expires_never")
    if expiry.is_due(now):
        return texts.get("expires_now")
    days = _days_between(now, expiry.at)
    if days <= 0:
        return texts.get("expires_today")
    if days == 1:
        return texts.get("expires_tomorrow")
    return texts.get("expires_in", days=days)


def expiry_long(texts: Texts, shipment: Shipment, expiry: Expiry, now: datetime) -> str:
    """Ausfuehrliche Begruendung fuer die Detailansicht."""
    if expiry.rule is ExpiryRule.CLOSED:
        reason = texts.get("rule_closed", since=format_date(shipment.closed_at))
    else:
        reason = texts.get("rule_stale", since=format_date(shipment.last_updated))
    if expiry.at is None:
        return f"{reason} {texts.get('rule_off')}"
    removal = texts.get("rule_at", date=format_date(expiry.at))
    return f"{reason} {removal} ({expiry_short(texts, expiry, now)})"


def destination_name(destinations: Mapping[str, str], destination_id: str | None) -> str:
    """Anzeigename eines Zielorts; ohne Zuordnung „Unbekannt"."""
    if not destination_id or destination_id == DESTINATION_UNKNOWN:
        return DESTINATION_UNKNOWN_NAME
    return destinations.get(destination_id, destination_id)


def carrier_name(carriers: Mapping[str, str], carrier_id: str) -> str:
    """Anzeigename eines Zustellers."""
    return carriers.get(carrier_id, carrier_id)


def shipment_title(shipment: Shipment) -> str:
    """Wodurch der Nutzer eine Sendung wiedererkennt: Artikel, Bestell- oder Trackingnummer."""
    return (
        shipment.description
        or next(iter(shipment.items), None)
        or shipment.tracking_number
        or shipment.order_number
        or shipment.id
    )


def shipment_label(
    texts: Texts,
    shipment: Shipment,
    expiry: Expiry,
    *,
    carriers: Mapping[str, str],
    destinations: Mapping[str, str],
    now: datetime,
) -> str:
    """Eine Zeile der Auswahlliste — reiner Text, kein Markdown."""
    parts = [
        carrier_name(carriers, shipment.carrier),
        texts.status(shipment.status.value),
        f"„{' '.join(shipment_title(shipment).split())[:60]}“",
        f"→ {destination_name(destinations, shipment.destination_id)}",
        texts.get("last_mail", age=age_text(texts, shipment.last_updated, now)),
        expiry_short(texts, expiry, now),
    ]
    return " · ".join(parts)


def overview(
    texts: Texts,
    shipments: list[Shipment],
    expiries: Mapping[str, Expiry],
    *,
    now: datetime,
) -> str:
    """Kopfzeile der Liste: Anzahl, ohne Zielort, beim naechsten Durchlauf weg."""
    without = sum(1 for item in shipments if item.destination_id in (None, DESTINATION_UNKNOWN))
    due = sum(1 for item in shipments if expiries[item.id].is_due(now))
    return texts.get("overview", total=len(shipments), without=without, due=due)


def shipment_details(
    texts: Texts,
    shipment: Shipment,
    expiry: Expiry,
    *,
    carriers: Mapping[str, str],
    destinations: Mapping[str, str],
    now: datetime,
) -> dict[str, str]:
    """Platzhalter der Detailansicht: Titel, Fakten, Verfall, Verlauf, Mails."""
    facts: list[tuple[str, str | None]] = [
        ("field_carrier", carrier_name(carriers, shipment.carrier)),
        ("field_status", _status_line(texts, shipment)),
        ("field_tracking_number", _tracking(shipment)),
        ("field_order_number", md(shipment.order_number) if shipment.order_number else None),
        ("field_sender", md(shipment.sender) if shipment.sender else None),
        ("field_items", md("; ".join(shipment.items)) if shipment.items else None),
        ("field_destination", md(destination_name(destinations, shipment.destination_id))),
        (
            "field_destination_raw",
            md(shipment.destination_raw) if shipment.destination_raw else texts.get("none_found"),
        ),
        ("field_pickup", _pickup(shipment)),
        ("field_eta", _eta(shipment)),
        ("field_delivered_at", format_dt(shipment.delivered_at) if shipment.delivered_at else None),
        ("field_first_seen", format_dt(shipment.first_seen)),
        ("field_last_updated", format_dt(shipment.last_updated)),
        ("field_id", f"`{shipment.id}`"),
    ]
    lines = [f"- **{texts.get(key)}:** {value}" for key, value in facts if value]
    return {
        "title": md(shipment_title(shipment)),
        "facts": "\n".join(lines),
        "expiry": expiry_long(texts, shipment, expiry, now),
        "events": _events_table(texts, shipment),
        "sources": _sources_table(texts, shipment),
    }


def _status_line(texts: Texts, shipment: Shipment) -> str:
    status = texts.status(shipment.status.value)
    if shipment.status_text and shipment.status_text.casefold() != status.casefold():
        return f"{status} ({md(shipment.status_text)})"
    return status


def _tracking(shipment: Shipment) -> str | None:
    if not shipment.tracking_number:
        return None
    number = md(shipment.tracking_number)
    if shipment.tracking_url:
        return f"[{number}]({shipment.tracking_url})"
    return number


def _pickup(shipment: Shipment) -> str | None:
    parts = [md(value) for value in (shipment.pickup_location, shipment.pickup_code) if value]
    return " · ".join(parts) or None


def _eta(shipment: Shipment) -> str | None:
    if shipment.eta_start is None:
        return None
    if shipment.eta_end is None or shipment.eta_end == shipment.eta_start:
        return format_dt(shipment.eta_start)
    return f"{format_dt(shipment.eta_start)} - {format_dt(shipment.eta_end)}"


def _header(texts: Texts, *columns: str) -> str:
    """Kopf einer Markdown-Tabelle samt Trennzeile."""
    titles = " | ".join(texts.get(column) for column in columns)
    return f"| {titles} |\n|{'---|' * len(columns)}"


def _events_table(texts: Texts, shipment: Shipment) -> str:
    if not shipment.events:
        return texts.get("no_events")
    rows = [_header(texts, "col_time", "col_status", "col_text")]
    for event in sorted(shipment.events, key=lambda item: item.timestamp, reverse=True):
        detail = " · ".join(
            md(value, MAX_CELL_LENGTH) for value in (event.text, event.location) if value
        )
        status = texts.status(event.status.value)
        rows.append(f"| {format_dt(event.timestamp)} | {status} | {detail or EMPTY} |")
    return "\n".join(rows)


def _sources_table(texts: Texts, shipment: Shipment) -> str:
    if not shipment.sources:
        return texts.get("no_sources")
    rows = [_header(texts, "col_time", "col_source", "col_subject", "col_from")]
    for record in sorted(shipment.sources, key=lambda item: item.fetched_at, reverse=True):
        subject = md(record.subject, MAX_CELL_LENGTH) if record.subject else EMPTY
        sender = md(record.sender, MAX_CELL_LENGTH) if record.sender else EMPTY
        rows.append(
            f"| {format_dt(record.fetched_at)} | {md(record.source_id)} | {subject} | {sender} |"
        )
    return "\n".join(rows)


def mail_log(
    texts: Texts,
    logs: Iterable[tuple[str, list[MailLogEntry]]],
    *,
    only_problems: bool,
    carriers: Mapping[str, str],
) -> str:
    """Die letzten Mails je Postfach als Tabelle, neueste zuerst."""
    sections: list[str] = []
    for title, entries in logs:
        shown = [
            entry
            for entry in reversed(entries)
            if not only_problems or entry.outcome is not MailOutcome.MATCHED
        ]
        sections.append(f"### {md(title)}")
        if not shown:
            sections.append(texts.get("no_mails"))
            continue
        rows = [_header(texts, "col_time", "col_result", "col_subject", "col_from")]
        for entry in shown:
            rows.append(
                f"| {format_dt(entry.received_at or entry.processed_at)} "
                f"| {_outcome_cell(texts, entry, carriers)} "
                f"| {md(entry.subject, MAX_CELL_LENGTH) if entry.subject else EMPTY} "
                f"| {md(entry.sender, MAX_CELL_LENGTH) if entry.sender else EMPTY} |"
            )
        sections.append("\n".join(rows))
    return "\n\n".join(sections)


_OUTCOME_ICONS: Final = {
    MailOutcome.MATCHED: "✅",
    MailOutcome.WITHOUT_SHIPMENT: "⚠️",
    MailOutcome.UNMATCHED: "❔",
    MailOutcome.FAILED: "❌",
}


def _outcome_cell(texts: Texts, entry: MailLogEntry, carriers: Mapping[str, str]) -> str:
    parts = [f"{_OUTCOME_ICONS[entry.outcome]} {texts.outcome(entry.outcome)}"]
    if entry.carrier:
        parts.append(carrier_name(carriers, entry.carrier))
    parts.extend(texts.status(status) for _, status in entry.shipments)
    return " · ".join(parts)
