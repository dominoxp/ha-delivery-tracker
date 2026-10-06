"""Diagnostics-Download auf Entry- und Device-Ebene.

Der Download landet in Fehlerberichten auf GitHub, also oeffentlich. Deshalb
zwei Schichten:

1. Aufbau: Geheime und personenbezogene Felder kommen gar nicht erst hinein.
   Zugangsdaten werden per `async_redact_data` maskiert, Regeln der Zielorte
   nur gezaehlt, Sendungen nur in Zahlen und mit dem *Format* ihrer
   Trackingnummer („AA999…") statt des Werts beschrieben — fuer die
   Fehlersuche an einem Regelsatz reicht das.
2. Netz: Zum Schluss wird jeder String nach den bekannten Geheimnissen
   (Benutzernamen, Passwoerter, Schluessel, Trackingnummern, Abholcodes,
   Adressregeln) und nach Mailadressen durchsucht. Das faengt auch freie Texte
   wie Fehlermeldungen oder Subentry-Titel („max@example.org (Pakete)") ab.

Die wichtigste Kennzahl beim Erweitern um neue Zusteller ist
`mail_stats.discarded`: Steigt sie, kommen Mails an, die kein Regelsatz
versteht. Zusammen mit der Action `test_parser` ergibt das den Arbeitsablauf
fuer neue Regelsaetze. `shipments.without_destination` zeigt entsprechend
eine fehlende Adressregel an.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .carriers import async_get_registry
from .const import (
    CONF_API_KEY,
    CONF_DESTINATION_RULES,
    CONF_PASSWORD,
    CONF_USERNAME,
    DESTINATION_UNKNOWN,
    SUBENTRY_API_SOURCE,
    SUBENTRY_DESTINATION,
    SUBENTRY_MAILBOX,
)
from .coordinator import SourceHandle
from .entry import (
    DeliveryTrackerConfigEntry,
    destination_device_identifier,
    destination_from_subentry,
)
from .models import Destination, Shipment
from .pipeline import PipelineStats
from .sources import ImapSource

# Felder in Subentry-Daten, die nie im Klartext erscheinen.
TO_REDACT = {CONF_PASSWORD, CONF_API_KEY, CONF_USERNAME}

# Kuerzere Werte werden im Netz nicht ersetzt: „1" oder „DE" kaemen sonst in
# jedem Zeitstempel und jeder Zahl unter die Raeder.
_MIN_SECRET_LENGTH = 4
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DeliveryTrackerConfigEntry
) -> dict[str, Any]:
    """Diagnose des ganzen Entries."""
    data = await _async_entry_diagnostics(hass, entry)
    return _scrub(data, _secrets(entry))


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: DeliveryTrackerConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Diagnose eines Devices: Zielort mit seinen Sendungen, sonst der ganze Entry."""
    for subentry in entry.get_subentries_of_type(SUBENTRY_DESTINATION):
        destination = destination_from_subentry(subentry)
        if destination_device_identifier(entry, destination.id) in device.identifiers:
            data = _destination_diagnostics(entry, subentry.subentry_id, destination)
            return _scrub(data, _secrets(entry))
    return await async_get_config_entry_diagnostics(hass, entry)


# ----------------------------------------------------------------------
# Abschnitte
# ----------------------------------------------------------------------


async def _async_entry_diagnostics(
    hass: HomeAssistant, entry: DeliveryTrackerConfigEntry
) -> dict[str, Any]:
    """Alle Abschnitte des Entry-Downloads."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    data = coordinator.data
    discovered = await async_get_registry(hass)

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "options": dict(entry.options),
        },
        "config": {
            "mailboxes": [
                {"subentry_id": subentry.subentry_id, **async_redact_data(subentry.data, TO_REDACT)}
                for subentry in entry.get_subentries_of_type(SUBENTRY_MAILBOX)
            ],
            "destinations": [
                _destination_config(subentry.subentry_id, destination_from_subentry(subentry))
                for subentry in entry.get_subentries_of_type(SUBENTRY_DESTINATION)
            ],
            "api_sources": [
                {"subentry_id": subentry.subentry_id, **async_redact_data(subentry.data, TO_REDACT)}
                for subentry in entry.get_subentries_of_type(SUBENTRY_API_SOURCE)
            ],
        },
        "carriers": {
            "discovered": discovered.carrier_ids,
            "active": runtime.registry.carrier_ids,
        },
        "sources": {key: _source_diagnostics(handle) for key, handle in runtime.sources.items()},
        "mail_stats": _mail_stats(
            handle.pipeline.stats for handle in runtime.sources.values() if handle.pipeline
        ),
        "shipments": _shipment_diagnostics(data.shipments),
        "last_update": data.updated_at.isoformat(),
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(coordinator.last_exception) if coordinator.last_exception else None,
    }


def _destination_diagnostics(
    entry: DeliveryTrackerConfigEntry, subentry_id: str, destination: Destination
) -> dict[str, Any]:
    """Device-Download eines Zielorts: Regeln (gezaehlt) und seine Sendungen."""
    shipments = [
        shipment
        for shipment in entry.runtime_data.coordinator.data.shipments
        if shipment.destination_id == destination.id
    ]
    return {
        "destination": _destination_config(subentry_id, destination),
        "shipments": _shipment_diagnostics(shipments),
    }


def _destination_config(subentry_id: str, destination: Destination) -> dict[str, Any]:
    """Ein Zielort ohne seine Regelwerte — Name, Strasse, Mail sind Adressdaten."""
    return {
        "subentry_id": subentry_id,
        "id": destination.id,
        "rule_counts": {rule: len(getattr(destination, rule)) for rule in CONF_DESTINATION_RULES},
    }


def _source_diagnostics(handle: SourceHandle) -> dict[str, Any]:
    """Zustand, letzter Fehler und Kennzahlen einer Quelle."""
    result: dict[str, Any] = {
        **handle.status().as_dict(),
        "issue": handle.issue,
        "interval": handle.interval.total_seconds() if handle.interval else None,
    }
    if isinstance(handle.source, ImapSource):
        result["imap"] = handle.source.diagnostics()
    elif handle.source is not None and (details := handle.source.diagnostics()):
        # API-Quellen: u.a. das verbleibende Kontingent.
        result["api"] = details
    if handle.pipeline is not None:
        result["mail_stats"] = handle.pipeline.stats.to_dict()
    return result


def _mail_stats(stats: Iterable[PipelineStats]) -> dict[str, Any]:
    """Summe ueber alle Postfaecher seit dem letzten Laden des Entries.

    `discarded` sind alle Mails, aus denen keine Sendung wurde: niemand
    zustaendig, Zusteller ohne Sendung (meist Werbung) oder Fehler.
    """
    total = Counter[str]()
    by_carrier = Counter[str]()
    for item in stats:
        values = item.to_dict()
        by_carrier.update(values.pop("by_carrier"))
        total.update(values)
    keys = ("processed", "matched", "unmatched", "without_shipment", "failed", "shipments")
    result: dict[str, Any] = {key: total[key] for key in keys}
    result["discarded"] = total["unmatched"] + total["without_shipment"] + total["failed"]
    result["by_carrier"] = dict(sorted(by_carrier.items()))
    return result


def _shipment_diagnostics(shipments: list[Shipment]) -> dict[str, Any]:
    """Zaehler je Status und Zusteller und eine Kurzform jeder Sendung."""
    active = [shipment for shipment in shipments if shipment.is_active]
    return {
        "total": len(shipments),
        "active": len(active),
        "by_status": dict(sorted(Counter(item.status.value for item in shipments).items())),
        "by_carrier": dict(sorted(Counter(item.carrier for item in shipments).items())),
        "without_destination": sum(1 for item in shipments if _unassigned(item)),
        "active_without_destination": sum(1 for item in active if _unassigned(item)),
        "items": [_shipment_shape(shipment) for shipment in shipments],
    }


def _unassigned(shipment: Shipment) -> bool:
    """True, wenn keine Adressregel gegriffen hat."""
    return shipment.destination_id in (None, DESTINATION_UNKNOWN)


def _shipment_shape(shipment: Shipment) -> dict[str, Any]:
    """Eine Sendung ohne personenbezogene Werte.

    Die ID fehlt bewusst: Sie enthaelt die Trackingnummer.
    """
    return {
        "carrier": shipment.carrier,
        "status": shipment.status.value,
        "tracking_number": _shape(shipment.tracking_number),
        "destination_id": shipment.destination_id,
        "sources": shipment.source_ids,
        "has_eta": shipment.eta_start is not None,
        "has_destination_raw": bool(shipment.destination_raw),
        "has_pickup_code": bool(shipment.pickup_code),
        "has_order_number": bool(shipment.order_number),
        "items": len(shipment.items),
        "events": len(shipment.events),
        "first_seen": shipment.first_seen.isoformat(),
        "last_updated": shipment.last_updated.isoformat(),
    }


def _shape(value: str | None) -> str | None:
    """Format statt Wert: Ziffern werden „9", Buchstaben „A"."""
    if value is None:
        return None
    return "".join("9" if char.isdigit() else "A" if char.isalpha() else char for char in value)


# ----------------------------------------------------------------------
# Netz: bekannte Geheimnisse in allen Strings ersetzen
# ----------------------------------------------------------------------


def _secrets(entry: DeliveryTrackerConfigEntry) -> list[str]:
    """Alle Werte, die nirgends im Download stehen duerfen, laengste zuerst."""
    values: set[str] = set()
    for subentry in entry.subentries.values():
        for key in TO_REDACT:
            if value := subentry.data.get(key):
                values.add(str(value))
    for subentry in entry.get_subentries_of_type(SUBENTRY_DESTINATION):
        destination = destination_from_subentry(subentry)
        values.add(destination.name)
        for rule in CONF_DESTINATION_RULES:
            values.update(getattr(destination, rule))
    for shipment in entry.runtime_data.store.shipments:
        values.update(
            value
            for value in (
                shipment.tracking_number,
                shipment.pickup_code,
                shipment.destination_raw,
                shipment.order_number,
            )
            if value
        )
        values.update(shipment.recipient_emails)
    return sorted(
        (value for value in values if len(value) >= _MIN_SECRET_LENGTH), key=len, reverse=True
    )


def _scrub(data: Any, secrets: list[str]) -> Any:
    """Rekursiv jeden String von Geheimnissen und Mailadressen befreien."""
    if isinstance(data, dict):
        return {key: _scrub(value, secrets) for key, value in data.items()}
    if isinstance(data, list | tuple):
        return [_scrub(value, secrets) for value in data]
    if not isinstance(data, str) or data == REDACTED:
        return data
    for secret in secrets:
        data = data.replace(secret, REDACTED)
    return _EMAIL.sub(REDACTED, data)
