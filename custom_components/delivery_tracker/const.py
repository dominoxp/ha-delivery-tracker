"""Konstanten der Delivery-Tracker-Integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "delivery_tracker"

# Plattformen, die beim Entry-Setup geladen werden.
PLATFORMS: Final[list[Platform]] = [Platform.CALENDAR, Platform.SENSOR]

# Attribute der Sensoren. Beide sind vom Recorder ausgenommen (Step 2.3).
ATTR_SHIPMENTS: Final = "shipments"
ATTR_SOURCES: Final = "sources"

# Schluessel und Version des Storage-Files unter .storage/.
STORAGE_KEY: Final = f"{DOMAIN}.shipments"
STORAGE_VERSION: Final = 1
# Verzoegertes Schreiben: mehrere Aenderungen kurz hintereinander ergeben einen Write.
STORAGE_SAVE_DELAY: Final = 10

# Konfigurationsfelder des Entries.
CONF_NAME: Final = "name"

# Subentry-Typen: Die eigentliche Konfiguration liegt in Subentries am Hub-Entry.
SUBENTRY_MAILBOX: Final = "mailbox"
SUBENTRY_DESTINATION: Final = "destination"
SUBENTRY_API_SOURCE: Final = "api_source"

# Felder eines `mailbox`-Subentrys.
CONF_SERVER: Final = "server"
CONF_PORT: Final = "port"
CONF_SECURITY: Final = "security"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_FOLDER: Final = "folder"
CONF_USE_IDLE: Final = "use_idle"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_LOOKBACK_DAYS: Final = "lookback_days"

# Felder eines `destination`-Subentrys. Die Regel-Listen tragen dieselben Namen
# wie die Felder von `models.Destination`, die Daten lassen sich also direkt
# mit `Destination.from_dict` lesen.
CONF_DESTINATION_ID: Final = "id"
CONF_DESTINATION_RULES: Final = (
    "recipient_names",
    "streets",
    "postal_codes",
    "cities",
    "packstations",
    "post_numbers",
    "emails",
    "keywords",
)

# Felder eines `api_source`-Subentrys.
CONF_SOURCE_TYPE: Final = "source_type"
CONF_API_KEY: Final = "api_key"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# Options des Hub-Entrys (globale Einstellungen).
CONF_RETENTION_DAYS: Final = "retention_days"
CONF_STALE_DAYS: Final = "stale_days"
CONF_ENABLED_CARRIERS: Final = "enabled_carriers"
"""Nur Formularfeld: Gespeichert wird die Gegenmenge `CONF_DISABLED_CARRIERS`."""
CONF_DISABLED_CARRIERS: Final = "disabled_carriers"
"""Abgeschaltete Zusteller. Als Negativliste, damit neu hinzugekommene Plugins
nach einem Update automatisch aktiv sind."""
CONF_GENERIC_FALLBACK: Final = "generic_fallback"

# Carrier-ID des generischen Fallbacks; er hat im Options-Flow einen eigenen Schalter.
CARRIER_GENERIC: Final = "generic"

# Standardwerte.
DEFAULT_NAME: Final = "Delivery Tracker"
DEFAULT_RETENTION_DAYS: Final = 14
# Aktive Sendungen ohne Aktualisierung verfallen nach dieser Zeit (Step 2.2).
DEFAULT_STALE_DAYS: Final = 60
DEFAULT_GENERIC_FALLBACK: Final = True
DEFAULT_API_SCAN_INTERVAL: Final = 3600
# Grundtakt des Coordinators: Retention, Zustand der Quellen und der Puffer der
# Postfaecher, falls ein Push-Signal verloren ging. Neue Mails melden sich selbst.
COORDINATOR_UPDATE_INTERVAL: Final = 300
# Obergrenze fuer einen einzelnen Abruf — eine haengende Quelle darf die Runde
# der anderen nicht aufhalten.
SOURCE_FETCH_TIMEOUT: Final = 120.0
# Schluessel in den Reauth-Daten: welcher Subentry neue Zugangsdaten braucht.
REAUTH_SUBENTRY_ID: Final = "subentry_id"
# Obergrenze fuer den Verbindungstest im Config-Flow — ein nicht erreichbarer
# Server darf den Dialog nicht haengen lassen.
CONNECTION_TEST_TIMEOUT: Final = 20.0

# Standardwerte der IMAP-Quelle.
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

# Eingebauter Zielort fuer Sendungen, die keiner konfigurierten Adresse zuzuordnen
# sind. Konfigurierte Zielorte duerfen diese ID nicht verwenden.
DESTINATION_UNKNOWN: Final = "unknown"
DESTINATION_UNKNOWN_NAME: Final = "Unbekannt"

# IDs der Datenquellen (Werte landen in `SourceRecord.source_id`).
SOURCE_IMAP: Final = "imap"
SOURCE_SEVENTEENTRACK: Final = "seventeentrack"

# Quellentypen, die als `api_source` angelegt werden koennen.
API_SOURCE_TYPES: Final = (SOURCE_SEVENTEENTRACK,)

# Wertigkeit der Quellen beim Merge: Die API ist fuer den Status autoritativ,
# die Mail liefert vor allem Kontext. Verfeinert wird das in Step 3.2.
DEFAULT_SOURCE_PRIORITY: Final = 0
SOURCE_PRIORITIES: Final[dict[str, int]] = {
    SOURCE_IMAP: 10,
    SOURCE_SEVENTEENTRACK: 20,
}
