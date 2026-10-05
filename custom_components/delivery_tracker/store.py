"""Persistenz der Sendungen ueber den HA-Store inklusive Retention.

Der Store haelt alle bekannten Sendungen im Speicher und schreibt verzoegert nach
`.storage/delivery_tracker.shipments`. Zugriffe laufen im Event-Loop und sind
daher synchron; Schreiben passiert gebuendelt ueber `async_delay_save`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .addresses import AddressMatcher
from .const import (
    DEFAULT_RETENTION_DAYS,
    STORAGE_KEY,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .merge import has_status_changed, merge_shipments
from .models import Shipment

_LOGGER = logging.getLogger(__name__)


class ShipmentStore:
    """Speicher aller Sendungen eines Config-Entries."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        """Store anlegen; geladen wird erst mit `async_load`."""
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._shipments: dict[str, Shipment] = {}
        self.retention_days = retention_days

    async def async_load(self) -> None:
        """Gespeicherten Stand einlesen. Unlesbare Eintraege werden verworfen."""
        data = await self._store.async_load()
        self._shipments = {}
        if not data:
            return

        for raw in data.get("shipments") or []:
            try:
                shipment = Shipment.from_dict(raw)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Gespeicherte Sendung ist unlesbar und wird verworfen: %s", err)
                continue
            self._shipments[shipment.id] = shipment

        _LOGGER.debug("%d Sendungen aus dem Speicher geladen", len(self._shipments))

    @property
    def shipments(self) -> list[Shipment]:
        """Alle Sendungen, juengste Aenderung zuerst."""
        return sorted(self._shipments.values(), key=lambda item: item.last_updated, reverse=True)

    @property
    def active_shipments(self) -> list[Shipment]:
        """Alle Sendungen, die noch unterwegs sind."""
        return [shipment for shipment in self.shipments if shipment.is_active]

    def get(self, shipment_id: str) -> Shipment | None:
        """Eine Sendung nach ID holen."""
        return self._shipments.get(shipment_id)

    @callback
    def async_upsert(self, shipment: Shipment) -> tuple[Shipment, bool]:
        """Eine Sendung einfuegen oder mit dem bestehenden Stand zusammenfuehren.

        Gibt die gespeicherte Sendung und zurueck, ob sich ihr Status geaendert hat.
        """
        previous = self._shipments.get(shipment.id)
        merged = shipment if previous is None else merge_shipments(previous, shipment)
        self._shipments[merged.id] = merged
        self.async_schedule_save()
        return merged, has_status_changed(previous, merged)

    @callback
    def async_assign_destinations(self, matcher: AddressMatcher) -> list[str]:
        """Alle Sendungen ihren Zielorten neu zuordnen.

        Laeuft bei jeder Aktualisierung: Aendert der Nutzer eine Regel, ziehen
        bestehende Sendungen beim naechsten Durchlauf um. `last_updated` bleibt
        dabei stehen — an der Sendung selbst hat sich nichts getan.

        Gibt die IDs der umgezogenen Sendungen zurueck.
        """
        moved = [shipment.id for shipment in matcher.assign_all(self._shipments.values())]
        if moved:
            _LOGGER.debug("%d Sendungen einem anderen Zielort zugeordnet", len(moved))
            self.async_schedule_save()
        return moved

    @callback
    def async_remove(self, shipment_id: str) -> bool:
        """Eine Sendung loeschen. True, wenn es sie gab."""
        if self._shipments.pop(shipment_id, None) is None:
            return False
        self.async_schedule_save()
        return True

    @callback
    def async_purge(self, *, now: datetime | None = None) -> list[str]:
        """Abgeschlossene Sendungen aelter als die Retention entfernen.

        Gibt die IDs der entfernten Sendungen zurueck.
        """
        if self.retention_days <= 0:
            return []

        cutoff = (now or dt_util.utcnow()) - timedelta(days=self.retention_days)
        removed = [
            shipment.id
            for shipment in self._shipments.values()
            if shipment.is_closed and shipment.closed_at < cutoff
        ]
        for shipment_id in removed:
            del self._shipments[shipment_id]

        if removed:
            _LOGGER.debug("%d abgeschlossene Sendungen nach Retention entfernt", len(removed))
            self.async_schedule_save()
        return removed

    @callback
    def async_schedule_save(self) -> None:
        """Schreiben anstossen — verzoegert, damit Bursts nur einen Write ergeben."""
        self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)

    async def async_save(self) -> None:
        """Sofort schreiben (z.B. beim Entladen des Entries)."""
        await self._store.async_save(self._data_to_save())

    async def async_clear(self) -> None:
        """Alles verwerfen und die Datei entfernen."""
        self._shipments = {}
        await self._store.async_remove()

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        """Serialisierter Zustand fuer den Store."""
        return {"shipments": [shipment.to_dict() for shipment in self._shipments.values()]}
