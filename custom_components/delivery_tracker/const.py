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

# Standardwerte der IMAP-Quelle. Die Konfigurationsfelder dazu kommen in Step 2.1.
DEFAULT_IMAP_PORT_SSL: Final = 993
DEFAULT_IMAP_PORT_PLAIN: Final = 143
DEFAULT_IMAP_FOLDER: Final = "INBOX"
# Suchkriterium fuer neue Mails. `\Seen` ist die dauerhafte Markierung „verarbeitet".
DEFAULT_IMAP_SEARCH: Final = "UNSEEN"
DEFAULT_IMAP_POLL_INTERVAL: Final = 300
DEFAULT_IMAP_LOOKBACK_DAYS: Final = 3
# Obergrenze der Mails pro Durchlauf, damit ein grosses Postfach den Loop nicht blockiert.
DEFAULT_IMAP_MAX_MESSAGES: Final = 25
DEFAULT_IMAP_COMMAND_TIMEOUT: Final = 10.0

# IDLE wird von Servern nach ca. 29 Minuten getrennt; vorher selbst erneuern.
IMAP_IDLE_REFRESH: Final = 1500.0
# Reconnect-Backoff: Verbindungsverlust ist Normalbetrieb, kein Fehlerzustand.
IMAP_BACKOFF_START: Final = 5.0
IMAP_BACKOFF_MAX: Final = 300.0
IMAP_BACKOFF_FACTOR: Final = 2.0

# Obergrenze der Event-Historie je Sendung, damit der Store nicht unbegrenzt waechst.
MAX_EVENTS_PER_SHIPMENT: Final = 50

# Obergrenze der Artikelliste je Sendung. Ein Paket mit mehr Positionen gibt es,
# aber fuer die Zuordnung in Step 3.2 reichen die ersten.
MAX_ITEMS_PER_SHIPMENT: Final = 20

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
