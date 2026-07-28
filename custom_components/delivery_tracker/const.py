"""Konstanten der Delivery-Tracker-Integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "delivery_tracker"

# Plattformen, die beim Entry-Setup geladen werden. Wird ab Phase 2 gefuellt.
PLATFORMS: Final[list[Platform]] = []

# Schluessel und Version des Storage-Files unter .storage/.
STORAGE_KEY: Final = f"{DOMAIN}.shipments"
STORAGE_VERSION: Final = 1

# Konfigurationsfelder des Entries.
CONF_NAME: Final = "name"

# Standardwerte.
DEFAULT_NAME: Final = "Delivery Tracker"
DEFAULT_RETENTION_DAYS: Final = 14
