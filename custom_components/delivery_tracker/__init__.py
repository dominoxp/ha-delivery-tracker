"""Delivery Tracker — Paketverfolgung aus E-Mails und APIs.

In diesem Schritt ist der Entry noch leer: Er wird angelegt, laedt (noch) keine
Plattformen und laesst sich rueckstandsfrei wieder entladen. Coordinator,
Persistenz und Entities kommen in den spaeteren Steps dazu.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Einen Config-Entry aufbauen."""
    _LOGGER.debug("Delivery Tracker wird eingerichtet: %s", entry.title)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Einen Config-Entry wieder abbauen."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Den Entry nach Options-Aenderungen neu laden."""
    await hass.config_entries.async_reload(entry.entry_id)
