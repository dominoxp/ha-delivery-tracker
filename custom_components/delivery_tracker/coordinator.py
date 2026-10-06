"""Der Coordinator: sammelt alle Quellen ein und bereitet die Sendungen auf.

Ein Durchlauf:

```
faellige Quellen ──▶ async_fetch (parallel, je Quelle isoliert)
                 ──▶ Store.async_upsert (Merge, Statuswechsel merken)
                 ──▶ Zuordnung zu Zielorten ──▶ Retention ──▶ CoordinatorData
```

Die Quellen liefern nur Neues, der Store haelt den Bestand — kein Durchlauf
parst alte Mails erneut. Postfaecher lesen in ihrem eigenen Hintergrund-Loop
(IDLE oder Polling) und melden neue Sendungen per `on_new_data`; das loest
sofort einen Durchlauf aus, statt auf das Intervall zu warten. Der Grundtakt
(`COORDINATOR_UPDATE_INTERVAL`) bleibt fuer Retention, API-Quellen und als
Sicherheitsnetz, falls ein Push-Signal verloren geht.

Fehler bleiben bei ihrer Quelle: Jede Quelle hat einen eigenen Zustand
(`SourceState`) und ihren letzten Fehler, die Ergebnisse der anderen kommen
trotzdem an. Dauerhafte Fehler (falsche Zugangsdaten, fehlender Ordner) werden
nicht wiederholt — bei Zugangsdaten startet stattdessen der Reauth-Flow.

Warum dafuer kein `ConfigEntryAuthFailed` aus `_async_update_data`: HA wertet
die Exception als Fehlschlag des *ganzen* Coordinators — alle Entities werden
unavailable und der Takt bleibt stehen. Ein Postfach mit altem Passwort wuerde
damit die uebrigen Quellen mitreissen. Der Coordinator stoesst den Reauth-Flow
deshalb selbst an (`ConfigEntry.async_start_reauth`, genau das, was HA bei der
Exception tut) und liefert die Daten der anderen Quellen weiter aus.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .addresses import AddressMatcher
from .carriers import CarrierRegistry
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SOURCE_TYPE,
    COORDINATOR_UPDATE_INTERVAL,
    DEFAULT_API_SCAN_INTERVAL,
    DESTINATION_UNKNOWN,
    DOMAIN,
    REAUTH_SUBENTRY_ID,
    SOURCE_FETCH_TIMEOUT,
    SOURCE_IMAP,
    SUBENTRY_API_SOURCE,
    SUBENTRY_MAILBOX,
)
from .dates import local_day_range
from .models import Destination, Shipment, ShipmentStatus
from .pipeline import MailPipeline
from .sources import (
    API_SOURCE_FACTORIES,
    ImapConfig,
    ImapProgress,
    ImapSource,
    SourceAuthError,
    SourcePermanentError,
    TrackingSource,
)
from .store import ShipmentStore

if TYPE_CHECKING:
    from .entry import DeliveryTrackerConfigEntry

_LOGGER = logging.getLogger(__name__)

# Status, die der Sensor „Problem" in Step 2.3 zusammenfasst.
PROBLEM_STATUSES = frozenset(
    {ShipmentStatus.DELAYED, ShipmentStatus.DELIVERY_FAILED, ShipmentStatus.RETURNED}
)


class SourceState(StrEnum):
    """Zustand einer Quelle aus Sicht des Coordinators."""

    PENDING = "pending"
    """Noch kein Abruf gelaufen."""
    OK = "ok"
    ERROR = "error"
    """Voruebergehender Fehler — wird im naechsten Durchlauf erneut versucht."""
    AUTH_FAILED = "auth_failed"
    """Zugangsdaten abgelehnt; ruht bis zum Reauth."""
    FAILED = "failed"
    """Sonstiger dauerhafter Fehler (Ordner, unbekannter Quellentyp); ruht bis zur Korrektur."""

    @property
    def is_permanent(self) -> bool:
        """True, wenn ein erneuter Versuch ohne Konfigurationsaenderung zwecklos ist."""
        return self in (SourceState.AUTH_FAILED, SourceState.FAILED)


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Momentaufnahme einer Quelle — Grundlage fuer Diagnose-Sensor und Repairs."""

    key: str
    """ID des Subentrys, aus dem die Quelle stammt."""
    kind: str
    title: str
    state: SourceState
    connection: str | None = None
    """Verbindungszustand, soweit die Quelle einen hat (IMAP: `ImapState`)."""
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_success: datetime | None = None
    failures: int = 0
    """Fehlschlaege in Folge; 0 nach jedem erfolgreichen Abruf."""

    def as_dict(self) -> dict[str, Any]:
        """Serialisiert fuer Attribute und Diagnostics."""
        return {
            "key": self.key,
            "kind": self.kind,
            "title": self.title,
            "state": self.state.value,
            "connection": self.connection,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "failures": self.failures,
        }


@dataclass(slots=True)
class SourceHandle:
    """Eine konfigurierte Quelle samt ihrem Laufzeitzustand."""

    key: str
    kind: str
    title: str
    source: TrackingSource | None
    """`None`, wenn sich die Quelle nicht bauen liess (dann ist `state` dauerhaft)."""
    interval: timedelta | None = None
    """Mindestabstand zwischen zwei Abrufen; `None` heisst jeder Durchlauf."""
    pipeline: MailPipeline | None = None
    state: SourceState = SourceState.PENDING
    started: bool = False
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_success: datetime | None = None
    last_fetch: datetime | None = None
    failures: int = 0

    def is_due(self, now: datetime) -> bool:
        """True, wenn die Quelle in diesem Durchlauf abgerufen werden soll."""
        if self.source is None or self.state.is_permanent:
            return False
        if self.interval is None or self.last_fetch is None:
            return True
        return now - self.last_fetch >= self.interval

    def status(self) -> SourceStatus:
        """Momentaufnahme fuer `CoordinatorData`."""
        connection = self.source.state.value if isinstance(self.source, ImapSource) else None
        return SourceStatus(
            key=self.key,
            kind=self.kind,
            title=self.title,
            state=self.state,
            connection=connection,
            last_error=self.last_error,
            last_error_at=self.last_error_at,
            last_success=self.last_success,
            failures=self.failures,
        )


@dataclass(frozen=True, slots=True)
class StatusChange:
    """Ein Statuswechsel aus einem Durchlauf — Step 2.5 feuert daraus die Events."""

    shipment_id: str
    previous: ShipmentStatus | None
    """`None` bei einer neu aufgetauchten Sendung."""
    current: ShipmentStatus
    shipment: Shipment


def shipment_sort_key(shipment: Shipment) -> tuple[Any, ...]:
    """Festgelegte Reihenfolge aller Listen in `CoordinatorData`.

    Zuerst Sendungen mit erwarteter Zustellung, die naechste zuerst
    (`eta_start` aufsteigend); danach die ohne Termin, juengste Aenderung
    zuerst. Die ID entscheidet Gleichstaende, damit die Liste stabil bleibt.
    Die Karte aus Step 4.2 verlaesst sich auf diese Reihenfolge.
    """
    if shipment.eta_start is not None:
        return (0, shipment.eta_start.timestamp(), shipment.id)
    return (1, -shipment.last_updated.timestamp(), shipment.id)


@dataclass(frozen=True, slots=True)
class CoordinatorData:
    """Ergebnis eines Durchlaufs, fertig gruppiert fuer die Entities.

    Alle Listen sind nach `shipment_sort_key` sortiert. Die Gruppen nach
    Zusteller und Zielort enthalten nur aktive Sendungen, `by_status` alle.
    Zeitbezogene Gruppen („heute") sind Methoden, weil sie zum Tageswechsel
    ohne neuen Durchlauf weiterruecken muessen.
    """

    shipments: list[Shipment]
    active: list[Shipment]
    by_carrier: dict[str, list[Shipment]]
    by_destination: dict[str, list[Shipment]]
    by_status: dict[ShipmentStatus, list[Shipment]]
    sources: dict[str, SourceStatus]
    updated_at: datetime
    status_changes: list[StatusChange] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    """IDs der Sendungen, die in diesem Durchlauf der Retention zum Opfer fielen."""

    def with_status(self, *statuses: ShipmentStatus) -> list[Shipment]:
        """Alle Sendungen mit einem der Status, in der festgelegten Reihenfolge."""
        found = [shipment for status in statuses for shipment in self.by_status.get(status, [])]
        return sorted(found, key=shipment_sort_key)

    @property
    def problems(self) -> list[Shipment]:
        """Verspaetet, gescheitert oder auf dem Rueckweg."""
        return self.with_status(*PROBLEM_STATUSES)

    def expected_on(self, day: date) -> list[Shipment]:
        """Aktive Sendungen, deren Zeitfenster den Tag (Ortszeit) beruehrt."""
        found = []
        for shipment in self.active:
            if shipment.eta_start is None:
                continue
            start, end = local_day_range(shipment.eta_start, shipment.eta_end)
            if start <= day <= end:
                found.append(shipment)
        return found

    def delivered_on(self, day: date) -> list[Shipment]:
        """Sendungen, die am Tag (Ortszeit) zugestellt wurden."""
        return [
            shipment
            for shipment in self.by_status.get(ShipmentStatus.DELIVERED, [])
            if shipment.delivered_at is not None
            and dt_util.as_local(shipment.delivered_at).date() == day
        ]


def build_data(
    shipments: Iterable[Shipment],
    *,
    sources: Mapping[str, SourceStatus],
    now: datetime,
    status_changes: Iterable[StatusChange] = (),
    removed: Iterable[str] = (),
) -> CoordinatorData:
    """Die Gruppierungen fuer die Entities einmal je Durchlauf vorberechnen."""
    ordered = sorted(shipments, key=shipment_sort_key)
    active = [shipment for shipment in ordered if shipment.is_active]

    by_carrier: dict[str, list[Shipment]] = {}
    by_destination: dict[str, list[Shipment]] = {}
    for shipment in active:
        by_carrier.setdefault(shipment.carrier, []).append(shipment)
        by_destination.setdefault(shipment.destination_id or DESTINATION_UNKNOWN, []).append(
            shipment
        )

    by_status: dict[ShipmentStatus, list[Shipment]] = {}
    for shipment in ordered:
        by_status.setdefault(shipment.status, []).append(shipment)

    return CoordinatorData(
        shipments=ordered,
        active=active,
        by_carrier=by_carrier,
        by_destination=by_destination,
        by_status=by_status,
        sources=dict(sources),
        updated_at=now,
        status_changes=list(status_changes),
        removed=list(removed),
    )


class DeliveryTrackerCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Fuehrt alle Quellen eines Entries zusammen."""

    config_entry: DeliveryTrackerConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DeliveryTrackerConfigEntry,
        *,
        store: ShipmentStore,
        registry: CarrierRegistry,
        destinations: Iterable[Destination],
    ) -> None:
        """Coordinator anlegen und die Quellen aus den Subentries bauen.

        Gestartet wird erst mit `async_start`; bis dahin liefert `data` den
        Stand aus dem Store.
        """
        destinations = list(destinations)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=COORDINATOR_UPDATE_INTERVAL),
        )
        self.store = store
        self._registry = registry
        self._destinations = destinations
        self._matcher = AddressMatcher(destinations)
        self._lock = asyncio.Lock()
        self._closed = False

        self.sources: dict[str, SourceHandle] = {}
        for subentry in entry.get_subentries_of_type(SUBENTRY_MAILBOX):
            self.sources[subentry.subentry_id] = self._create_mailbox(subentry)
        for subentry in entry.get_subentries_of_type(SUBENTRY_API_SOURCE):
            self.sources[subentry.subentry_id] = self._create_api_source(subentry)

        intervals = [handle.interval for handle in self.sources.values() if handle.interval]
        if intervals:
            self.update_interval = min([self.update_interval, *intervals])

        # Fortschritt geloeschter Postfaecher wird nicht mehr gebraucht.
        store.async_prune_source_states(
            {key for key, handle in self.sources.items() if handle.kind == SUBENTRY_MAILBOX}
        )
        self.data = self._build_data(dt_util.utcnow())

    # ------------------------------------------------------------------
    # Quellen bauen
    # ------------------------------------------------------------------

    def _create_mailbox(self, subentry: ConfigSubentry) -> SourceHandle:
        """Ein Postfach mit eigener Pipeline (eigene Kennzahlen je Postfach)."""
        handle = SourceHandle(
            key=subentry.subentry_id, kind=SUBENTRY_MAILBOX, title=subentry.title, source=None
        )
        try:
            config = ImapConfig.from_mapping(subentry.data)
            saved = self.store.source_state(subentry.subentry_id)
            progress = ImapProgress.from_dict(saved) if saved else None
        except (KeyError, TypeError, ValueError) as err:
            self._mark_failed(handle, SourceState.FAILED, f"Konfiguration unlesbar: {err}")
            return handle

        pipeline = MailPipeline(
            self._registry, source_id=SOURCE_IMAP, destinations=self._destinations
        )
        handle.pipeline = pipeline
        handle.source = ImapSource(
            config,
            handler=pipeline.handle,
            progress=progress,
            on_new_data=self._async_on_new_data,
            task_factory=self._create_task,
            source_id=SOURCE_IMAP,
        )
        return handle

    def _create_api_source(self, subentry: ConfigSubentry) -> SourceHandle:
        """Eine API-Quelle ueber die Fabrik ihres Typs."""
        source_type = str(subentry.data.get(CONF_SOURCE_TYPE, ""))
        interval = timedelta(
            seconds=int(subentry.data.get(CONF_SCAN_INTERVAL, DEFAULT_API_SCAN_INTERVAL))
        )
        handle = SourceHandle(
            key=subentry.subentry_id,
            kind=SUBENTRY_API_SOURCE,
            title=subentry.title,
            source=None,
            interval=interval,
        )
        factory = API_SOURCE_FACTORIES.get(source_type)
        if factory is None:
            self._mark_failed(
                handle, SourceState.FAILED, f"Quellentyp {source_type!r} wird nicht unterstuetzt"
            )
            return handle
        try:
            handle.source = factory(self.hass, subentry.data)
        except Exception as err:
            _LOGGER.exception("API-Quelle %s liess sich nicht anlegen", subentry.title)
            self._mark_failed(handle, SourceState.FAILED, str(err) or type(err).__name__)
        return handle

    def _create_task(self, coro: Any, name: str) -> asyncio.Task[None]:
        """Hintergrund-Tasks haengen am Entry und werden beim Unload abgeraeumt."""
        return self.config_entry.async_create_background_task(self.hass, coro, name)

    # ------------------------------------------------------------------
    # Lebenszyklus
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Hintergrund-Loops der Postfaecher starten, ohne auf sie zu warten.

        Der erste Abruf kann wegen des Lookbacks lange dauern; er laeuft im
        Loop der Quelle und meldet sich ueber `on_new_data`.
        """
        for handle in self.sources.values():
            if isinstance(handle.source, ImapSource):
                await handle.source.async_start()
                handle.started = True

    async def async_shutdown(self) -> None:
        """Takt stoppen, alle Quellen schliessen und den Fortschritt sichern.

        Laeuft beim Unload zweimal (explizit und ueber `async_on_unload`).
        """
        await super().async_shutdown()
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            *(self._async_close(handle) for handle in self.sources.values() if handle.source)
        )
        self._async_persist_progress()

    async def _async_close(self, handle: SourceHandle) -> None:
        """Eine Quelle schliessen; Fehler dabei halten die anderen nicht auf."""
        assert handle.source is not None
        try:
            await handle.source.async_close()
        except Exception:
            _LOGGER.exception("Quelle %s liess sich nicht sauber schliessen", handle.title)

    @callback
    def _async_on_new_data(self) -> None:
        """Push einer Quelle: sofort aktualisieren statt auf das Intervall zu warten."""
        if self._closed:
            return
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN} push refresh"
        )

    # ------------------------------------------------------------------
    # Ein Durchlauf
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> CoordinatorData:
        """Faellige Quellen abrufen, zusammenfuehren, zuordnen und bereinigen."""
        async with self._lock:
            now = dt_util.utcnow()
            due = [handle for handle in self.sources.values() if handle.is_due(now)]
            results = await asyncio.gather(*(self._async_fetch(handle, now) for handle in due))

            changes: list[StatusChange] = []
            for shipments in results:
                for incoming in shipments:
                    changes.extend(self._async_upsert(incoming))

            self.store.async_assign_destinations(self._matcher)
            removed = self.store.async_purge(now=now)
            self._async_persist_progress()
            return self._build_data(now, changes, removed)

    @callback
    def _async_upsert(self, incoming: Shipment) -> list[StatusChange]:
        """Eine Sendung in den Store mergen; liefert den Statuswechsel, falls es einen gab."""
        previous = self.store.get(incoming.id)
        previous_status = previous.status if previous is not None else None
        merged, changed = self.store.async_upsert(incoming)
        if not changed:
            return []
        return [StatusChange(merged.id, previous_status, merged.status, merged)]

    async def _async_fetch(self, handle: SourceHandle, now: datetime) -> list[Shipment]:
        """Eine Quelle abrufen. Wirft nie — der Fehler bleibt an der Quelle haengen."""
        source = handle.source
        assert source is not None
        handle.last_fetch = now
        try:
            async with asyncio.timeout(SOURCE_FETCH_TIMEOUT):
                if not handle.started:
                    await source.async_setup()
                    handle.started = True
                shipments = await source.async_fetch()
        except SourceAuthError as err:
            self._mark_failed(handle, SourceState.AUTH_FAILED, str(err))
            _LOGGER.error("%s: Zugangsdaten abgelehnt — bitte neu anmelden", handle.title)
            self.config_entry.async_start_reauth(self.hass, data={REAUTH_SUBENTRY_ID: handle.key})
            return []
        except SourcePermanentError as err:
            self._mark_failed(handle, SourceState.FAILED, str(err))
            _LOGGER.error("%s: %s — die Quelle ruht bis zur Korrektur", handle.title, err)
            return []
        except Exception as err:
            if handle.state is not SourceState.ERROR:
                _LOGGER.warning("%s: Abruf fehlgeschlagen: %s", handle.title, err or repr(err))
            else:
                _LOGGER.debug("%s: Abruf weiter fehlgeschlagen: %r", handle.title, err)
            self._mark_failed(handle, SourceState.ERROR, str(err) or type(err).__name__)
            return []

        if handle.state is SourceState.ERROR:
            _LOGGER.info("%s: Abruf funktioniert wieder", handle.title)
        handle.state = SourceState.OK
        handle.failures = 0
        handle.last_success = now
        return shipments

    @staticmethod
    def _mark_failed(handle: SourceHandle, state: SourceState, message: str) -> None:
        """Fehlerzustand an der Quelle festhalten."""
        handle.state = state
        handle.last_error = message
        handle.last_error_at = dt_util.utcnow()
        handle.failures += 1

    @callback
    def _async_persist_progress(self) -> None:
        """IMAP-Fortschritt im Store ablegen, damit er einen Neustart ueberlebt."""
        for key, handle in self.sources.items():
            if isinstance(handle.source, ImapSource):
                self.store.async_set_source_state(key, handle.source.progress.to_dict())

    def _build_data(
        self,
        now: datetime,
        changes: Iterable[StatusChange] = (),
        removed: Iterable[str] = (),
    ) -> CoordinatorData:
        """Den aktuellen Stand fuer die Entities aufbereiten."""
        return build_data(
            self.store.shipments,
            sources={key: handle.status() for key, handle in self.sources.items()},
            now=now,
            status_changes=changes,
            removed=removed,
        )
