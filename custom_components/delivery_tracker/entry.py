"""Konfiguration aus Hub-Entry und Subentries lesen, plus die Laufzeitdaten.

Der Config-Flow schreibt, dieses Modul liest — damit Setup und Coordinator
nicht selbst in `entry.data`/`subentry.data` herumsuchen muessen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry, ConfigSubentry

from .carriers import CarrierRegistry
from .const import (
    CARRIER_GENERIC,
    CONF_DISABLED_CARRIERS,
    CONF_GENERIC_FALLBACK,
    CONF_RETENTION_DAYS,
    CONF_STALE_DAYS,
    DEFAULT_GENERIC_FALLBACK,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_STALE_DAYS,
    DOMAIN,
    SUBENTRY_DESTINATION,
    SUBENTRY_MAILBOX,
)
from .models import Destination
from .sources.imap_source import ImapConfig
from .store import ShipmentStore

if TYPE_CHECKING:
    from .coordinator import DeliveryTrackerCoordinator, SourceHandle


@dataclass(slots=True)
class DeliveryTrackerData:
    """Laufzeitdaten eines geladenen Entries (`entry.runtime_data`)."""

    store: ShipmentStore
    coordinator: DeliveryTrackerCoordinator
    options: GlobalOptions
    registry: CarrierRegistry
    """Nur die aktiven Carrier-Plugins — je Plugin gibt es einen Sensor."""
    destinations: list[Destination] = field(default_factory=list)

    @property
    def sources(self) -> dict[str, SourceHandle]:
        """Alle Quellen nach Subentry-ID, mit Zustand und letztem Fehler."""
        return self.coordinator.sources


type DeliveryTrackerConfigEntry = ConfigEntry[DeliveryTrackerData]


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    """Die globalen Einstellungen aus dem Options-Flow, mit Standardwerten."""

    retention_days: int = DEFAULT_RETENTION_DAYS
    stale_days: int = DEFAULT_STALE_DAYS
    disabled_carriers: frozenset[str] = frozenset()
    generic_fallback: bool = DEFAULT_GENERIC_FALLBACK

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> GlobalOptions:
        """Options eines Entries lesen; fehlende Werte bekommen den Standard."""
        options = entry.options
        return cls(
            retention_days=int(options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)),
            stale_days=int(options.get(CONF_STALE_DAYS, DEFAULT_STALE_DAYS)),
            disabled_carriers=frozenset(options.get(CONF_DISABLED_CARRIERS, ())),
            generic_fallback=bool(options.get(CONF_GENERIC_FALLBACK, DEFAULT_GENERIC_FALLBACK)),
        )

    def carrier_enabled(self, carrier_id: str) -> bool:
        """True, wenn das Plugin Mails auswerten darf."""
        if carrier_id == CARRIER_GENERIC:
            return self.generic_fallback
        return carrier_id not in self.disabled_carriers

    def filter_registry(self, registry: CarrierRegistry) -> CarrierRegistry:
        """Eine Registry nur mit den Plugins, die Mails auswerten duerfen.

        Die globale Registry bleibt unangetastet — sie ist pro HA-Instanz geteilt.
        """
        return CarrierRegistry(
            [plugin for plugin in registry.plugins if self.carrier_enabled(plugin.carrier_id)]
        )


def destination_from_subentry(subentry: ConfigSubentry) -> Destination:
    """Einen Zielort aus seinem Subentry lesen."""
    return Destination.from_dict(subentry.data)


def destinations_from_entry(entry: ConfigEntry) -> list[Destination]:
    """Alle Zielorte in der Reihenfolge, in der sie angelegt wurden.

    Die Reihenfolge entscheidet Gleichstaende in der Zuordnung (`addresses`).
    """
    return [
        destination_from_subentry(subentry)
        for subentry in entry.get_subentries_of_type(SUBENTRY_DESTINATION)
    ]


def mailbox_configs_from_entry(entry: ConfigEntry) -> dict[str, ImapConfig]:
    """IMAP-Konfiguration je `mailbox`-Subentry, nach Subentry-ID."""
    return {
        subentry.subentry_id: ImapConfig.from_mapping(subentry.data)
        for subentry in entry.get_subentries_of_type(SUBENTRY_MAILBOX)
    }


def hub_device_identifier(entry: ConfigEntry) -> tuple[str, str]:
    """Device-Identifier des Haupt-Devices, an dem die Zaehler haengen."""
    return (DOMAIN, entry.entry_id)


def destination_device_identifier(entry: ConfigEntry, destination_id: str) -> tuple[str, str]:
    """Device-Identifier eines Zielorts — haengt an seiner ID, nicht am Namen."""
    return (DOMAIN, f"{entry.entry_id}_destination_{destination_id}")
