"""Delivery Tracker — Paketverfolgung aus E-Mails und APIs.

Der Entry richtet bisher die Carrier-Registry, die Devices der Zielorte und den
Sendungs-Store ein. Coordinator, Quellen und Entities kommen in den spaeteren
Steps dazu.

Jede Aenderung an Options oder Subentries laedt den Entry ueber den
Update-Listener neu. Beim Laden werden alle gespeicherten Sendungen gegen die
aktuellen Zielorte bewertet — so ziehen die Sendungen eines geloeschten
Zielorts nach „Unbekannt" um, statt zu verschwinden.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .addresses import AddressMatcher
from .carriers import async_get_registry
from .const import PLATFORMS, SUBENTRY_DESTINATION
from .entry import (
    DeliveryTrackerConfigEntry,
    DeliveryTrackerData,
    GlobalOptions,
    destination_device_identifier,
    destination_from_subentry,
    destinations_from_entry,
)
from .store import ShipmentStore

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: DeliveryTrackerConfigEntry) -> bool:
    """Einen Config-Entry aufbauen."""
    _LOGGER.debug("Delivery Tracker wird eingerichtet: %s", entry.title)

    # Discovery laeuft nur beim allerersten Aufruf; danach kommt der Cache.
    registry = await async_get_registry(hass)
    _LOGGER.debug("Carrier-Plugins verfuegbar: %s", ", ".join(registry.carrier_ids) or "keine")

    options = GlobalOptions.from_entry(entry)
    destinations = destinations_from_entry(entry)
    _async_sync_destination_devices(hass, entry)

    store = ShipmentStore(hass, retention_days=options.retention_days)
    await store.async_load()
    # Zielorte koennen sich seit dem letzten Laden geaendert haben.
    if moved := store.async_assign_destinations(AddressMatcher(destinations)):
        _LOGGER.debug("%d Sendungen nach Aenderung der Zielorte neu zugeordnet", len(moved))

    entry.runtime_data = DeliveryTrackerData(store=store, destinations=destinations)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DeliveryTrackerConfigEntry) -> bool:
    """Einen Config-Entry wieder abbauen."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.store.async_save()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: DeliveryTrackerConfigEntry) -> None:
    """Den Entry nach Aenderungen an Options oder Subentries neu laden."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_sync_destination_devices(hass: HomeAssistant, entry: DeliveryTrackerConfigEntry) -> None:
    """Je Zielort ein Device, unter dem spaeter seine Sensoren haengen.

    Beim Loeschen eines Subentrys raeumt HA das zugehoerige Device selbst ab.
    """
    device_registry = dr.async_get(hass)
    for subentry in entry.get_subentries_of_type(SUBENTRY_DESTINATION):
        destination = destination_from_subentry(subentry)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry.subentry_id,
            identifiers={destination_device_identifier(entry, destination.id)},
            name=destination.name,
            entry_type=dr.DeviceEntryType.SERVICE,
        )
