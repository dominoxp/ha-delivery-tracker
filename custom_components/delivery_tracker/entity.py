"""Gemeinsame Basis aller Entities der Integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_SHIPMENTS, ATTR_SOURCES
from .coordinator import DeliveryTrackerCoordinator
from .entry import hub_device_identifier


class DeliveryTrackerEntity(CoordinatorEntity[DeliveryTrackerCoordinator]):
    """Gemeinsame Basis: Name aus `translation_key`, Device des Hubs."""

    _attr_has_entity_name = True
    # Die Listen aendern sich mit jeder Sendung; sie gehoeren nicht in die
    # Recorder-Datenbank. Die Historie steckt im Zaehlerwert.
    _unrecorded_attributes = frozenset({ATTR_SHIPMENTS, ATTR_SOURCES})

    def __init__(self, coordinator: DeliveryTrackerCoordinator, key: str) -> None:
        """Entity am Haupt-Device anlegen."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={hub_device_identifier(entry)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )
