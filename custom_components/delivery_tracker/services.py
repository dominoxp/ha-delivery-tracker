"""Die Actions der Integration.

Registriert werden sie in `async_setup` — einmal je HA-Start, unabhaengig davon,
wie viele Entries es gibt oder wie oft einer neu geladen wird. Jede Action sucht
sich ihren Entry selbst: ueber `config_entry_id`, sonst alle geladenen.

| Action            | Antwort | Zweck                                            |
| ----------------- | ------- | ------------------------------------------------ |
| `get_shipments`   | ja      | Sendungen mit allen Details, gefiltert           |
| `add_shipment`    | nein    | Sendung von Hand anlegen                         |
| `remove_shipment` | nein    | Sendung loeschen                                 |
| `mark_delivered`  | nein    | Sendung als zugestellt markieren                 |
| `refresh`         | nein    | sofort abrufen, optional nur eine Quelle         |
| `test_parser`     | ja      | Mailtext durch die Plugins schicken (Regelbau)   |

`get_shipments` ersetzt die bewusst nicht gebauten Entities je Sendung; sie
muss ohne Vorwissen bedienbar sein und in einem trigger-basierten
Template-Sensor funktionieren. Fehleingaben enden deshalb in einer
uebersetzten `ServiceValidationError` statt in einem Traceback.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .carriers import CarrierPlugin, CarrierRegistry, async_get_registry
from .const import (
    ATTR_BODY,
    ATTR_CARRIER,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DESCRIPTION,
    ATTR_DESTINATION,
    ATTR_INCLUDE_EVENTS,
    ATTR_SENDER,
    ATTR_SHIPMENT_ID,
    ATTR_SOURCE,
    ATTR_STATUS,
    ATTR_SUBJECT,
    ATTR_TRACKING_NUMBER,
    DESTINATION_UNKNOWN,
    DESTINATION_UNKNOWN_NAME,
    DOMAIN,
    SERVICE_ADD_SHIPMENT,
    SERVICE_GET_SHIPMENTS,
    SERVICE_MARK_DELIVERED,
    SERVICE_REFRESH,
    SERVICE_REMOVE_SHIPMENT,
    SERVICE_TEST_PARSER,
    SOURCE_MANUAL,
)
from .coordinator import shipment_sort_key
from .entry import DeliveryTrackerConfigEntry
from .mail import mail_from_text
from .merge import build_shipment_id
from .models import Shipment, ShipmentStatus, SourceRecord
from .tracking import ChecksumResult, normalize_tracking_number

_LOGGER = logging.getLogger(__name__)

# Schluessel der uebersetzten Fehlermeldungen (`exceptions` in strings.json).
ERROR_NO_ENTRY = "no_entry"
ERROR_ENTRY_NOT_LOADED = "entry_not_loaded"
ERROR_MULTIPLE_ENTRIES = "multiple_entries"
ERROR_UNKNOWN_SHIPMENT = "unknown_shipment"
ERROR_UNKNOWN_DESTINATION = "unknown_destination"
ERROR_UNKNOWN_CARRIER = "unknown_carrier"
ERROR_UNKNOWN_SOURCE = "unknown_source"
ERROR_INVALID_TRACKING_NUMBER = "invalid_tracking_number"
ERROR_UNRECOGNIZED_TRACKING_NUMBER = "unrecognized_tracking_number"
ERROR_AMBIGUOUS_TRACKING_NUMBER = "ambiguous_tracking_number"

_ENTRY_ID = vol.Optional(ATTR_CONFIG_ENTRY_ID)
_STATUS_VALUES = [status.value for status in ShipmentStatus]

GET_SHIPMENTS_SCHEMA = vol.Schema(
    {
        _ENTRY_ID: cv.string,
        vol.Optional(ATTR_STATUS): vol.All(cv.ensure_list, [vol.In(_STATUS_VALUES)]),
        vol.Optional(ATTR_CARRIER): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_DESTINATION): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_INCLUDE_EVENTS, default=False): cv.boolean,
    }
)

ADD_SHIPMENT_SCHEMA = vol.Schema(
    {
        _ENTRY_ID: cv.string,
        vol.Required(ATTR_TRACKING_NUMBER): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional(ATTR_CARRIER): cv.string,
        vol.Optional(ATTR_DESTINATION): cv.string,
        vol.Optional(ATTR_DESCRIPTION): cv.string,
    }
)

SHIPMENT_ID_SCHEMA = vol.Schema(
    {
        _ENTRY_ID: cv.string,
        vol.Required(ATTR_SHIPMENT_ID): vol.All(cv.string, vol.Length(min=1)),
    }
)

REFRESH_SCHEMA = vol.Schema(
    {
        _ENTRY_ID: cv.string,
        vol.Optional(ATTR_SOURCE): cv.string,
    }
)

TEST_PARSER_SCHEMA = vol.Schema(
    {
        _ENTRY_ID: cv.string,
        vol.Optional(ATTR_SUBJECT, default=""): cv.string,
        vol.Optional(ATTR_SENDER, default=""): cv.string,
        vol.Required(ATTR_BODY): cv.string,
    }
)


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    """Eine uebersetzte Fehlermeldung fuer den Aufrufer."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Alle Actions registrieren — mehrfacher Aufruf ist harmlos."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_SHIPMENTS):
        return

    async def get_shipments(call: ServiceCall) -> ServiceResponse:
        return _get_shipments(hass, call.data)

    async def add_shipment(call: ServiceCall) -> None:
        _add_shipment(hass, call.data)

    async def remove_shipment(call: ServiceCall) -> None:
        entry, shipment_id = _entry_with_shipment(hass, call.data)
        entry.runtime_data.coordinator.async_remove_shipment(shipment_id)

    async def mark_delivered(call: ServiceCall) -> None:
        entry, shipment_id = _entry_with_shipment(hass, call.data)
        entry.runtime_data.coordinator.async_mark_delivered(shipment_id)

    async def refresh(call: ServiceCall) -> None:
        await _refresh(hass, call.data)

    async def test_parser(call: ServiceCall) -> ServiceResponse:
        return await _test_parser(hass, call.data)

    services = (
        (SERVICE_GET_SHIPMENTS, get_shipments, GET_SHIPMENTS_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_ADD_SHIPMENT, add_shipment, ADD_SHIPMENT_SCHEMA, SupportsResponse.NONE),
        (SERVICE_REMOVE_SHIPMENT, remove_shipment, SHIPMENT_ID_SCHEMA, SupportsResponse.NONE),
        (SERVICE_MARK_DELIVERED, mark_delivered, SHIPMENT_ID_SCHEMA, SupportsResponse.NONE),
        (SERVICE_REFRESH, refresh, REFRESH_SCHEMA, SupportsResponse.NONE),
        (SERVICE_TEST_PARSER, test_parser, TEST_PARSER_SCHEMA, SupportsResponse.ONLY),
    )
    for name, handler, schema, supports_response in services:
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=supports_response
        )


# ----------------------------------------------------------------------
# Entries finden
# ----------------------------------------------------------------------


def _entries(hass: HomeAssistant, data: dict[str, Any]) -> list[DeliveryTrackerConfigEntry]:
    """Der genannte Entry oder alle geladenen; ohne geladenen Entry ein Fehler."""
    if (entry_id := data.get(ATTR_CONFIG_ENTRY_ID)) is not None:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise _error(ERROR_NO_ENTRY)
        if entry.state is not ConfigEntryState.LOADED:
            raise _error(ERROR_ENTRY_NOT_LOADED, entry=entry.title)
        return [entry]

    entries: list[DeliveryTrackerConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise _error(ERROR_NO_ENTRY)
    return entries


def _single_entry(hass: HomeAssistant, data: dict[str, Any]) -> DeliveryTrackerConfigEntry:
    """Genau ein Entry — bei mehreren muss der Aufrufer waehlen."""
    entries = _entries(hass, data)
    if len(entries) > 1:
        raise _error(ERROR_MULTIPLE_ENTRIES)
    return entries[0]


def _entry_with_shipment(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[DeliveryTrackerConfigEntry, str]:
    """Den Entry, der die Sendung kennt."""
    shipment_id = data[ATTR_SHIPMENT_ID].strip()
    for entry in _entries(hass, data):
        if entry.runtime_data.store.get(shipment_id) is not None:
            return entry, shipment_id
    raise _error(ERROR_UNKNOWN_SHIPMENT, shipment_id=shipment_id)


def _destination_id(entry: DeliveryTrackerConfigEntry, value: str) -> str | None:
    """Einen Zielort ueber ID oder Namen finden (Gross-/Kleinschreibung egal)."""
    wanted = value.strip().casefold()
    if wanted in (DESTINATION_UNKNOWN, DESTINATION_UNKNOWN_NAME.casefold()):
        return DESTINATION_UNKNOWN
    for destination in entry.runtime_data.destinations:
        if wanted in (destination.id.casefold(), destination.name.casefold()):
            return destination.id
    return None


def _destination_names(entry: DeliveryTrackerConfigEntry) -> dict[str, str]:
    """Anzeigenamen aller Zielorte inklusive „Unbekannt"."""
    names = {destination.id: destination.name for destination in entry.runtime_data.destinations}
    names[DESTINATION_UNKNOWN] = DESTINATION_UNKNOWN_NAME
    return names


# ----------------------------------------------------------------------
# get_shipments
# ----------------------------------------------------------------------


def shipment_details(
    shipment: Shipment, *, include_events: bool, destination_name: str | None = None
) -> dict[str, Any]:
    """Die volle Darstellung einer Sendung fuer die Antwort von `get_shipments`.

    Wie `Shipment.to_dict`, aber ohne Schema-Versionen, die Quellen nur als
    IDs und die Event-Historie nur auf Wunsch — sie ist der grosse Brocken.
    """
    details = shipment.to_dict()
    del details["version"]
    details["destination_name"] = destination_name
    details["sources"] = shipment.source_ids
    events = details.pop("events")
    if include_events:
        details["events"] = [
            {key: value for key, value in event.items() if key != "version"} for event in events
        ]
    return details


def _get_shipments(hass: HomeAssistant, data: dict[str, Any]) -> ServiceResponse:
    """Sendungen aller (oder des genannten) Entries filtern.

    Ohne Statusfilter nur aktive Sendungen; wer abgeschlossene sehen will,
    nennt ihren Status ausdruecklich.
    """
    statuses = {ShipmentStatus(value) for value in data.get(ATTR_STATUS) or ()}
    carriers = {value.strip().casefold() for value in data.get(ATTR_CARRIER) or ()}
    include_events: bool = data[ATTR_INCLUDE_EVENTS]

    found: list[tuple[Shipment, dict[str, Any]]] = []
    for entry in _entries(hass, data):
        destinations = _resolve_destinations(entry, data.get(ATTR_DESTINATION))
        names = _destination_names(entry)
        coordinator_data = entry.runtime_data.coordinator.data
        pool = coordinator_data.shipments if statuses else coordinator_data.active
        for shipment in pool:
            if not _matches(shipment, statuses, carriers, destinations):
                continue
            details = shipment_details(
                shipment,
                include_events=include_events,
                destination_name=names.get(shipment.destination_id or DESTINATION_UNKNOWN),
            )
            details[ATTR_CONFIG_ENTRY_ID] = entry.entry_id
            found.append((shipment, details))

    found.sort(key=lambda item: shipment_sort_key(item[0]))
    return {"shipments": [details for _, details in found]}


def _matches(
    shipment: Shipment,
    statuses: set[ShipmentStatus],
    carriers: set[str],
    destinations: set[str] | None,
) -> bool:
    """Ob eine Sendung alle Filter erfuellt; ein leerer Filter laesst alles durch."""
    if statuses and shipment.status not in statuses:
        return False
    if carriers and shipment.carrier.casefold() not in carriers:
        return False
    destination_id = shipment.destination_id or DESTINATION_UNKNOWN
    return destinations is None or destination_id in destinations


def _resolve_destinations(
    entry: DeliveryTrackerConfigEntry, values: Iterable[str] | None
) -> set[str] | None:
    """Die Zielort-IDs eines Filters; `None` heisst „kein Filter"."""
    if not values:
        return None
    resolved: set[str] = set()
    for value in values:
        destination_id = _destination_id(entry, value)
        if destination_id is None:
            raise _error(ERROR_UNKNOWN_DESTINATION, destination=value)
        resolved.add(destination_id)
    return resolved


# ----------------------------------------------------------------------
# add_shipment
# ----------------------------------------------------------------------


def _add_shipment(hass: HomeAssistant, data: dict[str, Any]) -> Shipment:
    """Eine Sendung von Hand anlegen.

    Die ID entsteht genau wie bei einer Mail (`{carrier}_{nummer}`), damit eine
    spaeter eintreffende Mail dieselbe Sendung ergaenzt statt eine zweite
    anzulegen.
    """
    entry = _single_entry(hass, data)
    runtime = entry.runtime_data
    raw_number: str = data[ATTR_TRACKING_NUMBER]
    number = normalize_tracking_number(raw_number)
    plugin = _plugin_for_number(runtime.registry, number, raw_number, data.get(ATTR_CARRIER))
    carrier = plugin.carrier_for_number(number)

    override: str | None = None
    if (destination := data.get(ATTR_DESTINATION)) is not None:
        override = _destination_id(entry, destination)
        if override is None:
            raise _error(ERROR_UNKNOWN_DESTINATION, destination=destination)

    now = dt_util.utcnow()
    shipment = Shipment(
        id=build_shipment_id(carrier, number),
        carrier=carrier,
        tracking_number=number,
        description=data.get(ATTR_DESCRIPTION) or None,
        destination_override=override,
        tracking_url=plugin.tracking_url(number),
        sources=[SourceRecord(source_id=SOURCE_MANUAL, fetched_at=now)],
        first_seen=now,
        last_updated=now,
    )
    stored = runtime.coordinator.async_add_shipment(shipment)
    _LOGGER.debug("Sendung %s von Hand angelegt", stored.id)
    return stored


def _plugin_for_number(
    registry: CarrierRegistry, number: str, raw: str, carrier: str | None
) -> CarrierPlugin:
    """Das Plugin, dessen Format die Nummer traegt — dieselben Regeln wie bei Mails.

    Ohne Angabe des Zustellers muss die Nummer eindeutig sein: Eine stimmende
    Pruefziffer entscheidet, sonst darf nur ein Format passen. Raten waere hier
    schlimmer als nachfragen — eine falsch zugeordnete Sendung findet ihre
    Mails nie.
    """
    if carrier:
        plugin = registry.get(carrier.strip().lower())
        if plugin is None:
            raise _error(
                ERROR_UNKNOWN_CARRIER, carrier=carrier, carriers=", ".join(registry.carrier_ids)
            )
        result = plugin.check_tracking_number(number) if number else None
        if result is None or result is ChecksumResult.INVALID:
            raise _error(ERROR_INVALID_TRACKING_NUMBER, tracking_number=raw, carrier=plugin.name)
        return plugin

    found = registry.identify(number) if number else []
    if not found:
        raise _error(ERROR_UNRECOGNIZED_TRACKING_NUMBER, tracking_number=raw)
    valid = [plugin for plugin, result in found if result is ChecksumResult.VALID]
    if valid:
        return valid[0]
    if len(found) > 1:
        raise _error(
            ERROR_AMBIGUOUS_TRACKING_NUMBER,
            tracking_number=raw,
            carriers=", ".join(plugin.carrier_id for plugin, _ in found),
        )
    return found[0][0]


# ----------------------------------------------------------------------
# refresh
# ----------------------------------------------------------------------


async def _refresh(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Sofort abrufen; mit `source` nur die Quelle mit dieser ID oder diesem Namen."""
    entries = _entries(hass, data)
    source = data.get(ATTR_SOURCE)
    if not source:
        for entry in entries:
            await entry.runtime_data.coordinator.async_refresh_sources()
        return

    wanted = source.strip().casefold()
    for entry in entries:
        coordinator = entry.runtime_data.coordinator
        keys = [
            key
            for key, handle in coordinator.sources.items()
            if wanted in (key.casefold(), handle.title.casefold())
        ]
        if keys:
            await coordinator.async_refresh_sources(keys)
            return
    raise _error(ERROR_UNKNOWN_SOURCE, source=source)


# ----------------------------------------------------------------------
# test_parser
# ----------------------------------------------------------------------


async def _test_parser(hass: HomeAssistant, data: dict[str, Any]) -> ServiceResponse:
    """Einen Mailtext durch alle Plugins schicken und das Ergebnis offenlegen.

    Ohne geladenen Entry zaehlt die vollstaendige Registry, sonst die des
    Entries — mit dessen abgeschalteten Zustellern, denn genau so wuerde die
    echte Mail behandelt.
    """
    if data.get(ATTR_CONFIG_ENTRY_ID) or hass.config_entries.async_loaded_entries(DOMAIN):
        registry = _entries(hass, data)[0].runtime_data.registry
    else:
        registry = await async_get_registry(hass)

    mail = mail_from_text(data[ATTR_SUBJECT], data[ATTR_SENDER], data[ATTR_BODY])
    scores = {plugin.carrier_id: score for plugin, score in registry.candidates(mail)}
    resolution = registry.resolve(mail)
    winner = resolution.plugin if resolution.shipments else None

    return {
        "mail": {
            "subject": mail.subject,
            "from_address": mail.from_address,
            "from_domain": mail.from_domain,
            "text": mail.text,
        },
        "plugins": [
            {
                "carrier": plugin.carrier_id,
                "name": plugin.name,
                "score": scores.get(plugin.carrier_id, 0),
                "priority": plugin.priority,
            }
            for plugin in sorted(
                registry.plugins,
                key=lambda plugin: (-scores.get(plugin.carrier_id, 0), plugin.carrier_id),
            )
        ],
        "candidate": resolution.plugin.carrier_id if resolution.plugin else None,
        "winner": winner.carrier_id if winner else None,
        "failed": resolution.failed,
        "shipments": [
            shipment_details(shipment, include_events=True) for shipment in resolution.shipments
        ],
    }
