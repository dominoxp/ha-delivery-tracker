"""Sensoren: feste Zaehler mit der kompakten Sendungsliste als Attribut.

Bewusst keine Entity pro Sendung (siehe `planning.md`). Stattdessen:

- am Haupt-Device sechs Zaehler (aktiv, heute erwartet, in Zustellung,
  abholbereit, heute zugestellt, Probleme), je aktivem Zusteller ein Zaehler
  und zwei Diagnose-Sensoren (letzte Aktualisierung, Zustand der Quellen);
- je Zielort ein Zaehler an dessen Device.

Jeder Zaehler traegt seine Sendungen als Attribut `shipments` — die Liste aus
`Shipment.as_summary()`, ohne Event-Historie. **Sortierung** wie ueberall in
`CoordinatorData` (`coordinator.shipment_sort_key`): Sendungen mit Termin nach
`eta_start` aufsteigend, danach die ohne Termin, juengste Aenderung zuerst;
die ID entscheidet Gleichstaende. Die Karte aus Step 4.2 verlaesst sich darauf.

Die `unique_id`s haengen an Entry-ID, Carrier-ID bzw. Zielort-ID, nie an
Namen: Reload, Umbenennen und Ab-/Wiedereinschalten eines Zustellers aendern
sie nicht.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import ATTR_SHIPMENTS, ATTR_SOURCES, SUBENTRY_DESTINATION
from .coordinator import CoordinatorData, DeliveryTrackerCoordinator, SourceState
from .entity import DeliveryTrackerEntity
from .entry import (
    DeliveryTrackerConfigEntry,
    destination_device_identifier,
    destination_from_subentry,
)
from .models import Destination, Shipment, ShipmentStatus

# Der Coordinator rechnet zentral; die Entities lesen nur.
PARALLEL_UPDATES = 0

# Rangfolge fuer den Diagnose-Sensor: Der schlechteste Zustand aller Quellen
# ist der Zustand des Sensors.
_SOURCE_STATE_SEVERITY: dict[SourceState, int] = {
    SourceState.OK: 0,
    SourceState.PENDING: 1,
    SourceState.ERROR: 2,
    SourceState.FAILED: 3,
    SourceState.AUTH_FAILED: 4,
}


@dataclass(frozen=True, kw_only=True)
class ShipmentCountDescription(SensorEntityDescription):
    """Ein Zaehler am Haupt-Device: welche Sendungen er zaehlt."""

    shipments_fn: Callable[[CoordinatorData, date], list[Shipment]]
    """Sendungen des Zaehlers aus den Daten und dem heutigen Tag (Ortszeit)."""
    daily: bool = False
    """True, wenn das Ergebnis vom Tag abhaengt — dann zum Tageswechsel neu schreiben."""


COUNTERS: tuple[ShipmentCountDescription, ...] = (
    ShipmentCountDescription(
        key="active_shipments",
        translation_key="active_shipments",
        shipments_fn=lambda data, _today: data.active,
    ),
    ShipmentCountDescription(
        key="expected_today",
        translation_key="expected_today",
        shipments_fn=lambda data, today: data.expected_on(today),
        daily=True,
    ),
    ShipmentCountDescription(
        key="out_for_delivery",
        translation_key="out_for_delivery",
        shipments_fn=lambda data, _today: data.with_status(ShipmentStatus.OUT_FOR_DELIVERY),
    ),
    ShipmentCountDescription(
        key="ready_for_pickup",
        translation_key="ready_for_pickup",
        shipments_fn=lambda data, _today: data.with_status(ShipmentStatus.READY_FOR_PICKUP),
    ),
    ShipmentCountDescription(
        key="delivered_today",
        translation_key="delivered_today",
        shipments_fn=lambda data, today: data.delivered_on(today),
        daily=True,
    ),
    ShipmentCountDescription(
        key="problems",
        translation_key="problems",
        shipments_fn=lambda data, _today: data.problems,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeliveryTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Sensoren fuer den Hub und je Zielort anlegen."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator

    hub: list[SensorEntity] = [
        ShipmentCountSensor(coordinator, description) for description in COUNTERS
    ]
    hub.extend(
        CarrierSensor(coordinator, plugin.carrier_id, plugin.name)
        for plugin in runtime.registry.plugins
    )
    hub.append(LastUpdateSensor(coordinator))
    hub.append(SourcesSensor(coordinator))

    # Ein abgeschalteter Zusteller soll verschwinden, nicht als „nicht
    # verfuegbar" stehen bleiben. Schaltet man ihn wieder ein, entsteht der
    # Sensor mit derselben unique_id neu.
    _async_remove_stale_entities(
        hass, entry, {entity.unique_id for entity in hub if entity.unique_id}
    )
    async_add_entities(hub)

    # Zielorte haengen an ihrem Subentry: Loescht man ihn, raeumt HA Device
    # und Sensor selbst ab.
    for subentry in entry.get_subentries_of_type(SUBENTRY_DESTINATION):
        destination = destination_from_subentry(subentry)
        async_add_entities(
            [DestinationSensor(coordinator, destination)],
            config_subentry_id=subentry.subentry_id,
        )


@callback
def _async_remove_stale_entities(
    hass: HomeAssistant, entry: DeliveryTrackerConfigEntry, keep: set[str]
) -> None:
    """Hub-Sensoren aus der Registry entfernen, die es nicht mehr gibt."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.domain == Platform.SENSOR
            and entity.config_subentry_id is None
            and entity.unique_id not in keep
        ):
            registry.async_remove(entity.entity_id)


# ----------------------------------------------------------------------
# Basisklasse
# ----------------------------------------------------------------------


class ShipmentListSensor(DeliveryTrackerEntity, SensorEntity):
    """Ein Zaehler: Zustand ist die Anzahl, `shipments` die Liste dazu."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def shipments(self) -> list[Shipment]:
        """Die gezaehlten Sendungen, in der festgelegten Reihenfolge."""
        raise NotImplementedError

    @property
    def native_value(self) -> int:
        """Anzahl der Sendungen."""
        return len(self.shipments())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Die kompakte Sendungsliste — ohne Event-Historie und Rohtexte."""
        return {ATTR_SHIPMENTS: [shipment.as_summary() for shipment in self.shipments()]}


# ----------------------------------------------------------------------
# Zaehler
# ----------------------------------------------------------------------


class ShipmentCountSensor(ShipmentListSensor):
    """Einer der festen Zaehler am Haupt-Device."""

    entity_description: ShipmentCountDescription

    def __init__(
        self, coordinator: DeliveryTrackerCoordinator, description: ShipmentCountDescription
    ) -> None:
        """Zaehler aus seiner Beschreibung anlegen."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        """Tagesbezogene Zaehler ruecken um Mitternacht weiter, auch ohne neue Mail."""
        await super().async_added_to_hass()
        if self.entity_description.daily:
            self.async_on_remove(
                async_track_time_change(
                    self.hass, self._async_day_changed, hour=0, minute=0, second=0
                )
            )

    @callback
    def _async_day_changed(self, _now: datetime) -> None:
        """Neuer Tag: Zustand mit dem neuen Datum neu berechnen."""
        self.async_write_ha_state()

    def shipments(self) -> list[Shipment]:
        """Sendungen laut Beschreibung, bezogen auf den heutigen Tag."""
        return self.entity_description.shipments_fn(self.coordinator.data, dt_util.now().date())


class CarrierSensor(ShipmentListSensor):
    """Aktive Sendungen eines Zustellers."""

    _attr_translation_key = "carrier"

    def __init__(
        self, coordinator: DeliveryTrackerCoordinator, carrier_id: str, carrier_name: str
    ) -> None:
        """Sensor fuer ein Carrier-Plugin anlegen."""
        super().__init__(coordinator, f"carrier_{carrier_id}")
        self.carrier_id = carrier_id
        self._attr_translation_placeholders = {"carrier": carrier_name}

    def shipments(self) -> list[Shipment]:
        """Aktive Sendungen dieses Zustellers."""
        return self.coordinator.data.by_carrier.get(self.carrier_id, [])


class DestinationSensor(ShipmentListSensor):
    """Aktive Sendungen eines Zielorts, am Device des Zielorts."""

    _attr_translation_key = "destination"

    def __init__(self, coordinator: DeliveryTrackerCoordinator, destination: Destination) -> None:
        """Sensor am Device des Zielorts anlegen; die ID ueberlebt Umbenennen."""
        super().__init__(coordinator, f"destination_{destination.id}")
        self.destination_id = destination.id
        entry = coordinator.config_entry
        self._attr_device_info = DeviceInfo(
            identifiers={destination_device_identifier(entry, destination.id)},
            name=destination.name,
            entry_type=DeviceEntryType.SERVICE,
        )

    def shipments(self) -> list[Shipment]:
        """Aktive Sendungen fuer diesen Zielort."""
        return self.coordinator.data.by_destination.get(self.destination_id, [])


# ----------------------------------------------------------------------
# Diagnose
# ----------------------------------------------------------------------


class LastUpdateSensor(DeliveryTrackerEntity, SensorEntity):
    """Zeitpunkt des letzten Coordinator-Durchlaufs."""

    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DeliveryTrackerCoordinator) -> None:
        """Diagnose-Sensor anlegen."""
        super().__init__(coordinator, "last_update")

    @property
    def native_value(self) -> datetime:
        """Zeitpunkt aus dem Ergebnis des Durchlaufs."""
        return self.coordinator.data.updated_at


class SourcesSensor(DeliveryTrackerEntity, SensorEntity):
    """Zustand der Quellen: der schlechteste aller Quellen, Details als Attribut."""

    _attr_translation_key = "sources"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DeliveryTrackerCoordinator) -> None:
        """Diagnose-Sensor anlegen."""
        super().__init__(coordinator, "sources")
        self._attr_options = [state.value for state in SourceState]

    @property
    def native_value(self) -> str | None:
        """Schlechtester Zustand; ohne Quellen unbekannt."""
        states = [status.state for status in self.coordinator.data.sources.values()]
        if not states:
            return None
        return max(states, key=_SOURCE_STATE_SEVERITY.__getitem__).value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Je Quelle Zustand, Verbindung und letzter Fehler."""
        return {
            ATTR_SOURCES: [status.as_dict() for status in self.coordinator.data.sources.values()]
        }
