"""Events auf dem HA-Bus — die Schnittstelle fuer Automationen.

Quelle sind die `StatusChange`s eines Durchlaufs (bzw. eines Eingriffs ueber
eine Action). Ein Durchlauf ohne Aenderung liefert keine, und damit feuert auch
nichts: Ein Event je Abrufzyklus waere fuer Automationen wertlos.

| Event                                  | Wann                                    |
| -------------------------------------- | --------------------------------------- |
| `delivery_tracker_shipment_added`      | Sendung zum ersten Mal gesehen          |
| `delivery_tracker_shipment_updated`    | Status gewechselt (`old_status`/`new_status`) |
| `delivery_tracker_shipment_delivered`  | zusaetzlich, sobald der Status `delivered` wird |

Die Nutzdaten sind die kompakte Darstellung (`Shipment.as_summary`) plus
`config_entry_id`; die volle Historie gibt es ueber `get_shipments`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    EVENT_SHIPMENT_ADDED,
    EVENT_SHIPMENT_DELIVERED,
    EVENT_SHIPMENT_UPDATED,
)
from .coordinator import DeliveryTrackerCoordinator, StatusChange
from .models import ShipmentStatus

_LOGGER = logging.getLogger(__name__)

ATTR_OLD_STATUS = "old_status"
ATTR_NEW_STATUS = "new_status"


def events_for(change: StatusChange, entry_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Die Events zu einer Aenderung, in der Reihenfolge, in der sie feuern."""
    payload: dict[str, Any] = {**change.shipment.as_summary(), ATTR_CONFIG_ENTRY_ID: entry_id}
    events: list[tuple[str, dict[str, Any]]] = []
    if change.previous is None:
        events.append((EVENT_SHIPMENT_ADDED, payload))
    elif change.previous is not change.current:
        events.append(
            (
                EVENT_SHIPMENT_UPDATED,
                {
                    **payload,
                    ATTR_OLD_STATUS: change.previous.value,
                    ATTR_NEW_STATUS: change.current.value,
                },
            )
        )
    else:
        return events
    if change.current is ShipmentStatus.DELIVERED:
        events.append((EVENT_SHIPMENT_DELIVERED, payload))
    return events


def async_fire_events_listener(
    hass: HomeAssistant, coordinator: DeliveryTrackerCoordinator
) -> Callable[[], None]:
    """Coordinator-Listener, der die Aenderungen jeder Aktualisierung auf den Bus gibt."""
    entry_id = coordinator.config_entry.entry_id

    @callback
    def _listener() -> None:
        for change in coordinator.data.status_changes:
            for event_type, data in events_for(change, entry_id):
                _LOGGER.debug("%s: %s", event_type, change.shipment_id)
                hass.bus.async_fire(event_type, data)

    return _listener
