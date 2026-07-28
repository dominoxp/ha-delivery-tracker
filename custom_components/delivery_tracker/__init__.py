"""Delivery Tracker — Paketverfolgung aus E-Mails und APIs.

In diesem Schritt richtet der Entry nur die Carrier-Registry ein: Er laedt (noch)
keine Plattformen und laesst sich rueckstandsfrei wieder entladen. Coordinator,
Quellen und Entities kommen in den spaeteren Steps dazu.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .carriers import async_get_registry
from .const import PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Einen Config-Entry aufbauen."""
    _LOGGER.debug("Delivery Tracker wird eingerichtet: %s", entry.title)

    # Discovery laeuft nur beim allerersten Aufruf; danach kommt der Cache.
    registry = await async_get_registry(hass)
    _LOGGER.debug("Carrier-Plugins verfuegbar: %s", ", ".join(registry.carrier_ids) or "keine")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Einen Config-Entry wieder abbauen."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Den Entry nach Options-Aenderungen neu laden."""
    await hass.config_entries.async_reload(entry.entry_id)
