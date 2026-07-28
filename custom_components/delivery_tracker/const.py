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
# Verzoegertes Schreiben: mehrere Aenderungen kurz hintereinander ergeben einen Write.
STORAGE_SAVE_DELAY: Final = 10

# Konfigurationsfelder des Entries.
CONF_NAME: Final = "name"

# Standardwerte.
DEFAULT_NAME: Final = "Delivery Tracker"
DEFAULT_RETENTION_DAYS: Final = 14

# Obergrenze der Event-Historie je Sendung, damit der Store nicht unbegrenzt waechst.
MAX_EVENTS_PER_SHIPMENT: Final = 50

# IDs der Datenquellen (Werte landen in `SourceRecord.source_id`).
SOURCE_IMAP: Final = "imap"
SOURCE_SEVENTEENTRACK: Final = "seventeentrack"

# Wertigkeit der Quellen beim Merge: Die API ist fuer den Status autoritativ,
# die Mail liefert vor allem Kontext. Verfeinert wird das in Step 3.2.
DEFAULT_SOURCE_PRIORITY: Final = 0
SOURCE_PRIORITIES: Final[dict[str, int]] = {
    SOURCE_IMAP: 10,
    SOURCE_SEVENTEENTRACK: 20,
}
