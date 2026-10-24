"""Persistenz der Sendungen ueber den HA-Store inklusive Retention.

Der Store haelt alle bekannten Sendungen im Speicher und schreibt verzoegert nach
`.storage/delivery_tracker.shipments`. Zugriffe laufen im Event-Loop und sind
daher synchron; Schreiben passiert gebuendelt ueber `async_delay_save`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .addresses import AddressMatcher
from .const import (
    DEFAULT_RETENTION_DAYS,
    DEFAULT_STALE_DAYS,
    STORAGE_KEY,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .merge import has_status_changed, merge_shipments
from .models import Shipment

_LOGGER = logging.getLogger(__name__)


class ExpiryRule(StrEnum):
    """Welche Retention-Regel eine Sendung irgendwann entfernt."""

    CLOSED = "closed"
    """Abgeschlossen; laeuft ab `closed_at` ueber `retention_days`."""
    STALE = "stale"
    """Offen; laeuft ab `last_updated` ueber `stale_days`."""


@dataclass(frozen=True, slots=True)
class Expiry:
    """Wann und warum eine Sendung aus dem Store faellt."""

    rule: ExpiryRule
    at: datetime | None
    """`None`, wenn die Regel abgeschaltet ist (`0` Tage) — die Sendung bleibt dann."""

    def is_due(self, now: datetime) -> bool:
        """True, wenn die Sendung beim naechsten Durchlauf entfernt wird."""
        return self.at is not None and self.at < now


class ShipmentStore:
    """Speicher aller Sendungen eines Config-Entries."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        stale_days: int = DEFAULT_STALE_DAYS,
    ) -> None:
        """Store anlegen; geladen wird erst mit `async_load`."""
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._shipments: dict[str, Shipment] = {}
        self._source_states: dict[str, dict[str, Any]] = {}
        self._mail_logs: dict[str, list[dict[str, Any]]] = {}
        self.retention_days = retention_days
        self.stale_days = stale_days

    async def async_load(self) -> None:
        """Gespeicherten Stand einlesen. Unlesbare Eintraege werden verworfen."""
        data = await self._store.async_load()
        self._shipments = {}
        self._source_states = {}
        self._mail_logs = {}
        if not data:
            return

        logs = data.get("mail_logs")
        if isinstance(logs, dict):
            self._mail_logs = {
                str(key): [dict(item) for item in value if isinstance(item, dict)]
                for key, value in logs.items()
                if isinstance(value, list)
            }

        states = data.get("source_states")
        if isinstance(states, dict):
            self._source_states = {
                str(key): dict(value) for key, value in states.items() if isinstance(value, dict)
            }

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
    def async_replace(self, shipment: Shipment) -> None:
        """Eine Sendung ohne Merge ueberschreiben — fuer bewusste Eingriffe des Nutzers.

        Der Merge liesse einen Status nie zuruecklaufen; „manuell zugestellt"
        muss aber auch eine gescheiterte Zustellung abloesen koennen.
        """
        self._shipments[shipment.id] = shipment
        self.async_schedule_save()

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

    def expiry(self, shipment: Shipment) -> Expiry:
        """Wann die Retention die Sendung entfernt.

        Zwei Regeln, jede mit `0` abschaltbar:

        * abgeschlossen und laenger als `retention_days` her (ab `closed_at`),
        * nicht abgeschlossen und seit `stale_days` ohne Aktualisierung — sonst
          bliebe etwa eine Bestellbestaetigung ohne Versandmail fuer immer aktiv.
          Massgeblich ist `last_updated`, nicht `first_seen`: Eine lange laufende
          Sendung mit frischer Mail bleibt.

        `async_purge` und die Sendungsverwaltung nutzen beide diese Methode,
        damit Anzeige und tatsaechliches Loeschen nie auseinanderlaufen.
        """
        if shipment.is_closed:
            days, since, rule = self.retention_days, shipment.closed_at, ExpiryRule.CLOSED
        else:
            days, since, rule = self.stale_days, shipment.last_updated, ExpiryRule.STALE
        return Expiry(rule, since + timedelta(days=days) if days > 0 else None)

    @callback
    def async_purge(self, *, now: datetime | None = None) -> list[str]:
        """Verfallene Sendungen entfernen (Regeln siehe `expiry`).

        Gibt die IDs der entfernten Sendungen zurueck.
        """
        now = now or dt_util.utcnow()
        removed: list[str] = []
        stale = 0
        for shipment in self._shipments.values():
            expiry = self.expiry(shipment)
            if expiry.is_due(now):
                removed.append(shipment.id)
                stale += expiry.rule is ExpiryRule.STALE
        for shipment_id in removed:
            del self._shipments[shipment_id]

        if removed:
            _LOGGER.debug(
                "%d Sendungen nach Retention entfernt, davon %d ohne Aktualisierung",
                len(removed),
                stale,
            )
            self.async_schedule_save()
        return removed

    @callback
    def async_set_destination_override(
        self, shipment_ids: list[str], destination_id: str
    ) -> list[str]:
        """Sendungen fest einem Zielort zuordnen. Gibt die gefundenen IDs zurueck.

        `destination_id` wird gleich mitgesetzt; die Neubewertung beim naechsten
        Durchlauf bestaetigt das, sobald es den Zielort gibt.
        """
        changed: list[str] = []
        for shipment_id in shipment_ids:
            if (shipment := self._shipments.get(shipment_id)) is None:
                continue
            shipment.destination_override = destination_id
            shipment.destination_id = destination_id
            changed.append(shipment_id)
        if changed:
            self.async_schedule_save()
        return changed

    def source_state(self, key: str) -> dict[str, Any] | None:
        """Gespeicherter Zustand einer Quelle (z.B. IMAP-Fortschritt), falls vorhanden."""
        state = self._source_states.get(key)
        return dict(state) if state is not None else None

    @callback
    def async_set_source_state(self, key: str, state: dict[str, Any]) -> None:
        """Zustand einer Quelle merken; geschrieben wird nur bei einer Aenderung."""
        if self._source_states.get(key) == state:
            return
        self._source_states[key] = dict(state)
        self.async_schedule_save()

    @callback
    def async_prune_source_states(self, keep: set[str]) -> None:
        """Zustaende und Mail-Protokolle geloeschter Quellen verwerfen."""
        stale = set(self._source_states) - keep
        for key in stale:
            del self._source_states[key]
        stale_logs = set(self._mail_logs) - keep
        for key in stale_logs:
            del self._mail_logs[key]
        if stale or stale_logs:
            self.async_schedule_save()

    def mail_log(self, key: str) -> list[dict[str, Any]]:
        """Gespeichertes Protokoll der letzten Mails eines Postfachs, aelteste zuerst."""
        return [dict(item) for item in self._mail_logs.get(key, [])]

    @callback
    def async_set_mail_log(self, key: str, entries: list[dict[str, Any]]) -> None:
        """Protokoll eines Postfachs merken; geschrieben wird nur bei einer Aenderung."""
        if self._mail_logs.get(key) == entries:
            return
        self._mail_logs[key] = [dict(item) for item in entries]
        self.async_schedule_save()

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
        return {
            "shipments": [shipment.to_dict() for shipment in self._shipments.values()],
            "source_states": {key: dict(state) for key, state in self._source_states.items()},
            "mail_logs": {key: list(entries) for key, entries in self._mail_logs.items()},
        }
