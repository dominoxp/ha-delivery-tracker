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
nicht wiederholt — bei Zugangsdaten startet stattdessen der Reauth-Flow. Was
der Nutzer selbst beheben muss, bekommt ausserdem einen Repairs-Eintrag
(`issues`), der mit dem naechsten erfolgreichen Abruf wieder verschwindet.

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
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
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
    ISSUE_API_KEY_INVALID,
    ISSUE_API_QUOTA_EXHAUSTED,
    ISSUE_MAILBOX_AUTH_FAILED,
    ISSUE_MAILBOX_FOLDER_NOT_FOUND,
    REAUTH_SUBENTRY_ID,
    SOURCE_FETCH_TIMEOUT,
    SOURCE_IMAP,
    SOURCE_MANUAL,
    SUBENTRY_API_SOURCE,
    SUBENTRY_MAILBOX,
)
from .dates import local_day_range
from .issues import async_sync_source_issues
from .merge import merge_events, merge_sources
from .models import Destination, Shipment, ShipmentEvent, ShipmentStatus, SourceRecord
from .pipeline import MailLogEntry, MailPipeline
from .sources import (
    API_SOURCE_FACTORIES,
    ImapConfig,
    ImapFolderError,
    ImapProgress,
    ImapSource,
    SourceAuthError,
    SourceContext,
    SourcePermanentError,
    SourceQuotaError,
    SourceRateLimitError,
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
    issue: str | None = None
    """Repairs-Eintrag (`ISSUE_*`), solange die Quelle an einem behebbaren Fehler ruht."""

    @property
    def settled(self) -> bool:
        """True, wenn der Ausgang des letzten Abrufs feststeht.

        Ausstehend oder voruebergehend gestoert heisst: Ob ein frueherer
        dauerhafter Fehler behoben ist, weiss noch niemand.
        """
        return self.state not in (SourceState.PENDING, SourceState.ERROR)

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
    """Eine neue Sendung oder ein Statuswechsel — Quelle der Events auf dem HA-Bus."""

    shipment_id: str
    previous: ShipmentStatus | None
    """`None` bei einer neu aufgetauchten Sendung."""
    current: ShipmentStatus
    shipment: Shipment


def _permanent_issue(err: SourcePermanentError) -> str | None:
    """Repairs-Eintrag fuer einen dauerhaften Fehler, falls der Nutzer ihn beheben kann."""
    if isinstance(err, ImapFolderError):
        return ISSUE_MAILBOX_FOLDER_NOT_FOUND
    if isinstance(err, SourceQuotaError):
        return ISSUE_API_QUOTA_EXHAUSTED
    return None


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
        self._forced: set[str] | None = None
        """Quellen, die der naechste Durchlauf unabhaengig vom Intervall abruft."""

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
            self._registry,
            source_id=SOURCE_IMAP,
            destinations=self._destinations,
            log=self._stored_mail_log(subentry.subentry_id),
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

    def _stored_mail_log(self, key: str) -> list[MailLogEntry]:
        """Das gespeicherte Mail-Protokoll eines Postfachs; Unlesbares faellt weg."""
        entries: list[MailLogEntry] = []
        for item in self.store.mail_log(key):
            try:
                entries.append(MailLogEntry.from_dict(item))
            except KeyError, TypeError, ValueError, IndexError:
                continue
        return entries

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
        context = SourceContext(
            known_shipments=lambda: self.store.shipments, registry=self._registry
        )
        try:
            handle.source = factory(self.hass, subentry.data, context)
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
            forced, self._forced = self._forced, None
            if forced is None:
                due = [handle for handle in self.sources.values() if handle.is_due(now)]
            else:
                due = [
                    handle
                    for key, handle in self.sources.items()
                    if key in forced and handle.source is not None and not handle.state.is_permanent
                ]
            results = await asyncio.gather(*(self._async_fetch(handle, now) for handle in due))

            changes: list[StatusChange] = []
            for shipments in results:
                for incoming in shipments:
                    changes.extend(self._async_upsert(incoming))

            consolidated = self._async_consolidate()
            changes.extend(consolidated[0])
            self.store.async_assign_destinations(self._matcher)
            removed = [*consolidated[1], *self.store.async_purge(now=now)]
            self._async_persist_progress()
            async_sync_source_issues(self.hass, self.config_entry, self.sources)
            return self._build_data(now, changes, removed)

    @callback
    def _async_consolidate(self) -> tuple[list[StatusChange], list[str]]:
        """Doppelt gefuehrte Sendungen zusammenlegen; Statuswechsel und entfallene IDs."""
        changes: list[StatusChange] = []
        removed: list[str] = []
        for absorbed, before, after in self.store.async_consolidate():
            removed.append(absorbed)
            if before is None or before.status is not after.status:
                changes.append(
                    StatusChange(after.id, before.status if before else None, after.status, after)
                )
        return changes, removed

    @callback
    def _async_upsert(self, incoming: Shipment) -> list[StatusChange]:
        """Eine Sendung in den Store mergen.

        Liefert eine Aenderung, wenn die Sendung neu ist oder ihr Status
        gewechselt hat — eine Mail, die nur Bekanntes wiederholt, liefert nichts.
        Gehoert die Sendung zu einer schon gefuehrten (`ShipmentStore.async_route`),
        landet sie dort.
        """
        incoming = self.store.async_route(incoming)
        previous = self.store.get(incoming.id)
        previous_status = previous.status if previous is not None else None
        merged, changed = self.store.async_upsert(incoming)
        if previous is not None and not changed:
            return []
        return [StatusChange(merged.id, previous_status, merged.status, merged)]

    # ------------------------------------------------------------------
    # Eingriffe ueber die Actions
    # ------------------------------------------------------------------

    async def async_refresh_sources(self, keys: Collection[str] | None = None) -> None:
        """Sofort abrufen, ohne Ruecksicht auf das Intervall der Quellen.

        Ohne `keys` alle Quellen, sonst nur die genannten. Quellen mit einem
        dauerhaften Fehler bleiben aussen vor — sie brauchen eine Korrektur,
        keinen weiteren Versuch.
        """
        self._forced = set(self.sources) if keys is None else set(keys)
        await self.async_refresh()

    async def async_rescan_mailboxes(self) -> list[str]:
        """Alle Postfaecher den Lookback-Zeitraum neu lesen lassen.

        Das Mail-Protokoll wird dafuer geleert, damit jede Mail genau einmal
        darin steht. Sendungen werden wie bei jeder Mail zusammengefuehrt.
        Gibt die Titel der Postfaecher zurueck, die neu einlesen.
        """
        started: list[str] = []
        for handle in self.sources.values():
            if not isinstance(handle.source, ImapSource) or handle.state.is_permanent:
                continue
            if handle.pipeline is not None:
                handle.pipeline.clear_log()
            await handle.source.async_rescan()
            started.append(handle.title)
        self._async_persist_progress()
        if started:
            self._create_task(self.async_refresh_sources(), f"{DOMAIN} rescan")
        return started

    @callback
    def async_add_shipment(self, shipment: Shipment) -> Shipment:
        """Eine von Hand angelegte Sendung uebernehmen und sofort ausliefern.

        Gibt es die Sendung schon, wird wie bei einer Mail zusammengefuehrt.
        """
        changes = self._async_upsert(shipment)
        self.store.async_assign_destinations(self._matcher)
        self._async_publish(changes)
        stored = self.store.get(shipment.id)
        assert stored is not None
        return stored

    @callback
    def async_mark_delivered(self, shipment_id: str) -> Shipment | None:
        """Eine Sendung als zugestellt markieren; `None`, wenn es sie nicht gibt.

        Laeuft am Merge vorbei, weil der Status sonst etwa nach einer
        gescheiterten Zustellung nicht mehr auf „zugestellt" wechseln duerfte.
        """
        shipment = self.store.get(shipment_id)
        if shipment is None:
            return None
        if shipment.status is ShipmentStatus.DELIVERED:
            return shipment

        now = dt_util.utcnow()
        updated = replace(
            shipment,
            status=ShipmentStatus.DELIVERED,
            status_text=None,
            delivered_at=now,
            last_updated=now,
            events=merge_events(
                shipment.events, [ShipmentEvent(timestamp=now, status=ShipmentStatus.DELIVERED)]
            ),
            sources=merge_sources(
                shipment.sources, [SourceRecord(source_id=SOURCE_MANUAL, fetched_at=now)]
            ),
        )
        self.store.async_replace(updated)
        self._async_publish([StatusChange(shipment.id, shipment.status, updated.status, updated)])
        return updated

    @callback
    def async_remove_shipment(self, shipment_id: str) -> bool:
        """Eine Sendung loeschen. True, wenn es sie gab."""
        return bool(self.async_remove_shipments([shipment_id]))

    @callback
    def async_remove_shipments(self, shipment_ids: Iterable[str]) -> list[str]:
        """Mehrere Sendungen loeschen und einmal ausliefern. Gibt die geloeschten IDs zurueck."""
        removed = [item for item in shipment_ids if self.store.async_remove(item)]
        if removed:
            self._async_publish(removed=removed)
        return removed

    @callback
    def _async_publish(
        self, changes: Iterable[StatusChange] = (), removed: Iterable[str] = ()
    ) -> None:
        """Den aktuellen Stand sofort an Entities und Event-Listener geben."""
        self.async_set_updated_data(self._build_data(dt_util.utcnow(), changes, removed))

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
            handle.issue = (
                ISSUE_MAILBOX_AUTH_FAILED
                if handle.kind == SUBENTRY_MAILBOX
                else ISSUE_API_KEY_INVALID
            )
            _LOGGER.error("%s: Zugangsdaten abgelehnt — bitte neu anmelden", handle.title)
            self.config_entry.async_start_reauth(self.hass, data={REAUTH_SUBENTRY_ID: handle.key})
            return []
        except SourcePermanentError as err:
            self._mark_failed(handle, SourceState.FAILED, str(err))
            handle.issue = _permanent_issue(err)
            _LOGGER.error("%s: %s — die Quelle ruht bis zur Korrektur", handle.title, err)
            return []
        except SourceRateLimitError as err:
            # Drosselung ist kein Defekt: Die Quelle wartet ihren Backoff ab, kein Repairs-Eintrag.
            _LOGGER.debug("%s: gedrosselt: %s", handle.title, err)
            self._mark_failed(handle, SourceState.ERROR, str(err) or type(err).__name__)
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
        # Eine Quelle kann eingeschraenkt weiterlaufen (17TRACK ohne Kontingent):
        # Die Ergebnisse kommen an, der Hinweis bleibt trotzdem stehen.
        warning = source.warning
        handle.issue = (
            _permanent_issue(warning) if isinstance(warning, SourcePermanentError) else None
        )
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
        """IMAP-Fortschritt und Mail-Protokoll sichern, damit sie einen Neustart ueberleben."""
        for key, handle in self.sources.items():
            if isinstance(handle.source, ImapSource):
                self.store.async_set_source_state(key, handle.source.progress.to_dict())
            if handle.pipeline is not None:
                self.store.async_set_mail_log(
                    key, [entry.to_dict() for entry in handle.pipeline.log]
                )

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
