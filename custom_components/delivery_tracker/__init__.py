"""Delivery Tracker — Paketverfolgung aus E-Mails und APIs.

Beim Laden eines Entries:

1. Store laden — vor jedem Abruf, damit Reload und Neustart keine Sendung
   verlieren oder doppelt anlegen. Danach alle Sendungen gegen die aktuellen
   Zielorte bewerten: So ziehen die Sendungen eines geloeschten Zielorts nach
   „Unbekannt" um, statt zu verschwinden.
2. Coordinator mit den Quellen aus den `mailbox`- und `api_source`-Subentries
   anlegen und die Postfach-Loops starten.
3. Der erste Abruf laeuft im Hintergrund: Das Lookback eines Postfachs kann
   lange dauern und darf das Setup nicht aufhalten. Eine kaputte Quelle laesst
   das Setup ebenfalls nicht scheitern — ihr Fehler steht an der Quelle.

Jede Aenderung an Options oder Subentries laedt den Entry ueber den
Update-Listener neu. Die Actions haengen nicht am Entry, sondern werden in
`async_setup` einmal je HA-Start registriert (`services`).
"""

from __future__ import annotations

import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .addresses import AddressMatcher
from .carriers import async_get_registry
from .const import DOMAIN, PLATFORMS, SUBENTRY_DESTINATION
from .coordinator import DeliveryTrackerCoordinator
from .entry import (
    DeliveryTrackerConfigEntry,
    DeliveryTrackerData,
    GlobalOptions,
    destination_device_identifier,
    destination_from_subentry,
    destinations_from_entry,
)
from .events import async_fire_events_listener
from .issues import async_clear_issues
from .services import async_setup_services
from .store import ShipmentStore

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Die Actions registrieren — einmal je HA-Start, nicht je Entry.

    So ueberstehen sie Reloads, und mehrere Entries teilen sich dieselben.
    """
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DeliveryTrackerConfigEntry) -> bool:
    """Einen Config-Entry aufbauen."""
    _LOGGER.debug("Delivery Tracker wird eingerichtet: %s", entry.title)

    options = GlobalOptions.from_entry(entry)
    # Discovery laeuft nur beim allerersten Aufruf; danach kommt der Cache.
    registry = options.filter_registry(await async_get_registry(hass))
    _LOGGER.debug("Aktive Carrier-Plugins: %s", ", ".join(registry.carrier_ids) or "keine")

    destinations = destinations_from_entry(entry)
    _async_sync_destination_devices(hass, entry)

    store = ShipmentStore(
        hass, retention_days=options.retention_days, stale_days=options.stale_days
    )
    await store.async_load()
    # Zielorte koennen sich seit dem letzten Laden geaendert haben.
    if moved := store.async_assign_destinations(AddressMatcher(destinations)):
        _LOGGER.debug("%d Sendungen nach Aenderung der Zielorte neu zugeordnet", len(moved))

    coordinator = DeliveryTrackerCoordinator(
        hass, entry, store=store, registry=registry, destinations=destinations
    )
    entry.runtime_data = DeliveryTrackerData(
        store=store,
        coordinator=coordinator,
        options=options,
        registry=registry,
        destinations=destinations,
    )

    # Der Event-Listener haelt zugleich den Takt des Coordinators am Laufen,
    # auch bevor es Entities gibt.
    entry.async_on_unload(
        coordinator.async_add_listener(async_fire_events_listener(hass, coordinator))
    )

    # Beim Herunterfahren entlaedt HA die Entries nicht; die Postfach-
    # Verbindungen sollen trotzdem geordnet schliessen.
    async def _async_on_stop(_event: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop))
    await coordinator.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} first refresh {entry.entry_id}"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DeliveryTrackerConfigEntry) -> bool:
    """Einen Config-Entry wieder abbauen: Quellen schliessen, Store final schreiben."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = entry.runtime_data
        await data.coordinator.async_shutdown()
        await data.store.async_save()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: DeliveryTrackerConfigEntry) -> None:
    """Beim Loeschen des Entries seine Repairs-Eintraege mitnehmen."""
    async_clear_issues(hass, entry)


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
