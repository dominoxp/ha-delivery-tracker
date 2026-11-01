"""17TRACK-API als Quelle: Live-Status fuer Trackingnummern aus Mails und aus dem API-Konto.

Die offizielle Tracking-API ist kostenpflichtig: Jede registrierte Nummer
kostet eine Einheit Kontingent, und das kostenlose Startkontingent ist klein.
Ohne API-Schluessel arbeitet `seventeentrack_account_source` mit dem normalen
17TRACK-Benutzerkonto. Was beide teilen, steht in `SeventeenTrackSourceBase`.

Ein Durchlauf:

```
gettracklist ──▶ was verfolgt 17TRACK schon?          (kostenlos)
   │
   ├─▶ stoptrack    abgeschlossene Sendungen abmelden  (kostenlos)
   ├─▶ register     neue, gepruefte Nummern aus Mails  (kostet Kontingent)
   └─▶ gettrackinfo alle anderen aktiven Nummern       (kostenlos, je 40 gebuendelt)
                    └─▶ Fragmente; zugestellte gleich wieder abmelden
```

Kontingent-Schonung:

* **Registriert wird nur, was die Pruefung der Carrier-Plugins besteht** — beim
  generischen Fallback nur mit stimmender Pruefziffer. Fehltreffer kosten
  sonst Kontingent, das echten Sendungen fehlt.
* Abgelehnte Nummern werden erst nach `SEVENTEENTRACK_REJECT_RETRY` erneut versucht.
* Frisch registrierte Nummern werden erst im naechsten Durchlauf abgefragt —
  vorher hat 17TRACK ohnehin noch nichts beim Zusteller geholt.
* Abgeschlossene Sendungen werden abgemeldet und nicht weiter abgefragt.
* Wie oft das alles passiert, regelt das Intervall des Subentrys.

Fehler:

* Schluessel abgelehnt → `SeventeenTrackAuthError` (Reauth und Repairs ueber den Coordinator).
* Kontingent leer → die Quelle laeuft weiter, nur das Registrieren ruht; das
  meldet sie ueber `warning` (Repairs-Hinweis), bis wieder Kontingent da ist.
* Drosselung → exponentieller Backoff; bis zu dessen Ende wirft jeder Abruf
  denselben voruebergehenden Fehler, ohne die API zu beruehren.

Die Mail kennt Haendler, Produkt und Zieladresse, die API nicht: Die Fragmente
lassen diese Felder leer, damit der Merge sie nie ueberschreibt.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Self

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from ..const import (
    CARRIER_GENERIC,
    CONF_API_KEY,
    CONF_EXCLUDED_CARRIERS,
    DOMAIN,
    EVENT_TRACKING_REJECTED,
    SEVENTEENTRACK_BACKOFF_MAX,
    SEVENTEENTRACK_BACKOFF_START,
    SEVENTEENTRACK_REJECT_RETRY,
    SOURCE_MANUAL,
    SOURCE_SEVENTEENTRACK,
)
from ..merge import build_shipment_id
from ..models import Shipment, ShipmentEvent, ShipmentStatus, SourceRecord
from ..tracking import ChecksumResult, normalize_tracking_number
from .base import (
    API_SOURCE_FACTORIES,
    SourceContext,
    SourceError,
    SourceRateLimitError,
    TrackingReport,
    TrackingSource,
    TrackingState,
)
from .seventeentrack_api import (
    CODE_ALREADY_REGISTERED,
    CODE_QUOTA_EXHAUSTED,
    BatchResult,
    Quota,
    SeventeenTrackClient,
    SeventeenTrackQuotaError,
    TrackItem,
)

_LOGGER = logging.getLogger(__name__)

MAPPING_FILE: Final = Path(__file__).with_name("seventeentrack.yaml")
_MAPPING_KEY: Final = f"{DOMAIN}.seventeentrack_mapping"

# Laenge, die 17TRACK fuer eine Nummer akzeptiert.
_MIN_NUMBER_LENGTH: Final = 5
_MAX_NUMBER_LENGTH: Final = 50


# ----------------------------------------------------------------------
# Zuordnung (seventeentrack.yaml)
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeventeenTrackMapping:
    """Carrier-Codes und Status von 17TRACK in der Sprache dieser Integration."""

    carriers: dict[str, tuple[int, ...]] = field(default_factory=dict)
    statuses: dict[str, ShipmentStatus] = field(default_factory=dict)
    sub_statuses: dict[str, ShipmentStatus] = field(default_factory=dict)
    account_statuses: dict[int, ShipmentStatus] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Self:
        """Aus dem YAML-Inhalt bauen; Unbekanntes ist ein Fehler, kein stilles Raten."""
        carriers = {
            str(carrier_id): tuple(int(code) for code in codes)
            for carrier_id, codes in (raw.get("carriers") or {}).items()
        }
        statuses = {
            str(key): ShipmentStatus(value) for key, value in (raw.get("statuses") or {}).items()
        }
        sub_statuses = {
            str(key): ShipmentStatus(value)
            for key, value in (raw.get("sub_statuses") or {}).items()
        }
        account_statuses = {
            int(key): ShipmentStatus(value)
            for key, value in (raw.get("account_statuses") or {}).items()
        }
        return cls(
            carriers=carriers,
            statuses=statuses,
            sub_statuses=sub_statuses,
            account_statuses=account_statuses,
        )

    def carrier_code(self, carrier_id: str) -> int | None:
        """Der Code, mit dem eine Nummer dieses Zustellers registriert wird."""
        codes = self.carriers.get(carrier_id)
        return codes[0] if codes else None

    def carrier_id(self, code: Any) -> str | None:
        """Die eigene Carrier-ID zu einem 17TRACK-Code."""
        try:
            value = int(code)
        except TypeError, ValueError:
            return None
        for carrier_id, codes in self.carriers.items():
            if value in codes:
                return carrier_id
        return None

    def status(self, status: Any, sub_status: Any = None) -> ShipmentStatus:
        """Unterstatus vor Hauptstatus; Unbekanntes wird `unknown` und aendert nichts."""
        if isinstance(sub_status, str) and sub_status in self.sub_statuses:
            return self.sub_statuses[sub_status]
        if not isinstance(status, str) or not status:
            # Events tragen oft nur den Unterstatus (`InTransit_Arrival`).
            status = sub_status.split("_", 1)[0] if isinstance(sub_status, str) else ""
        return self.statuses.get(status, ShipmentStatus.UNKNOWN)

    def account_status(self, state: int) -> ShipmentStatus:
        """Status des Benutzerkontos; Unbekanntes wird `unknown` und aendert nichts."""
        return self.account_statuses.get(state, ShipmentStatus.UNKNOWN)


def load_mapping(path: Path = MAPPING_FILE) -> SeventeenTrackMapping:
    """Die Zuordnungsdatei lesen (blockierend — im Executor aufrufen)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"{path.name}: erwartet wird eine Zuordnung auf oberster Ebene")
    return SeventeenTrackMapping.from_dict(raw)


async def async_get_mapping(hass: HomeAssistant) -> SeventeenTrackMapping:
    """Die Zuordnung einmal je HA-Instanz laden."""
    if (mapping := hass.data.get(_MAPPING_KEY)) is None:
        mapping = await hass.async_add_executor_job(load_mapping)
        hass.data[_MAPPING_KEY] = mapping
    return mapping


# ----------------------------------------------------------------------
# Antworten lesen
# ----------------------------------------------------------------------


def _parse_time(value: Any) -> datetime | None:
    """ISO-Zeitstempel der API; ohne Zeitzone gilt UTC."""
    if not isinstance(value, str) or not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return dt_util.as_utc(parsed)


def _event_time(event: Mapping[str, Any]) -> datetime | None:
    """`time_utc` ist eindeutig; `time_iso` traegt die Ortszeit samt Versatz."""
    return _parse_time(event.get("time_utc")) or _parse_time(event.get("time_iso"))


def _location(value: Any) -> str | None:
    """Ort als Text — die API liefert ihn mal als Text, mal als Objekt."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        parts = [
            str(value[key]).strip()
            for key in ("city", "state", "country")
            if value.get(key) and str(value[key]).strip()
        ]
        return ", ".join(parts) or None
    return None


def _text(value: Any) -> str | None:
    """Freitext der API; leere Werte werden `None`."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


# ----------------------------------------------------------------------
# Die Quelle
# ----------------------------------------------------------------------


class SeventeenTrackSourceBase(TrackingSource, ABC):
    """Was API- und Konto-Quelle von 17TRACK teilen.

    Zuordnung, Drosselungs-Backoff, die Pruefung vor dem Eintragen einer
    Nummer und die Identitaet der Fragmente. Den eigentlichen Abgleich
    liefert die Unterklasse in `_async_sync`.
    """

    def __init__(
        self,
        context: SourceContext,
        *,
        hass: HomeAssistant | None = None,
        mapping: SeventeenTrackMapping | None = None,
        source_id: str,
        priority: int | None = None,
        excluded_carriers: Iterable[str] = (),
    ) -> None:
        """Quelle anlegen; die Zuordnung wird ohne Vorgabe in `async_setup` geladen."""
        super().__init__(source_id, priority=priority)
        self._context = context
        self._excluded = frozenset(excluded_carriers)
        """Zusteller, die diese Quelle weder eintraegt noch abfragt noch abmeldet."""
        self._hass = hass
        self._mapping = mapping
        self._backoff = 0.0
        self._backoff_until: datetime | None = None
        self._rejected: dict[str, datetime] = {}
        """Abgelehnte Nummern mit dem Zeitpunkt der Ablehnung."""
        self._tracked = 0
        self._report: dict[str, TrackingReport] | None = None
        """Abgleich des letzten vollstaendigen Durchlaufs; `None` vor dem ersten."""
        self._next_report: dict[str, TrackingReport] = {}
        """Abgleich des laufenden Durchlaufs; ersetzt `_report` erst am Ende."""

    @property
    def mapping(self) -> SeventeenTrackMapping:
        """Die geladene Zuordnung."""
        if self._mapping is None:
            raise SourceError("Zuordnung von 17TRACK ist nicht geladen")
        return self._mapping

    async def _async_load_mapping(self) -> None:
        """Die Zuordnung laden, falls keine vorgegeben wurde."""
        if self._mapping is None:
            if self._hass is None:
                raise SourceError("Ohne HA-Instanz laesst sich die Zuordnung nicht laden")
            self._mapping = await async_get_mapping(self._hass)

    def _backoff_diagnostics(self) -> dict[str, Any]:
        """Gemeinsamer Teil der Diagnostics."""
        return {
            "tracked": self._tracked,
            "excluded_carriers": sorted(self._excluded),
            "rejected_numbers": len(self._rejected),
            "backoff_seconds": self._backoff,
            "backoff_until": self._backoff_until.isoformat() if self._backoff_until else None,
        }

    async def async_fetch(self) -> list[Shipment]:
        """Ein Abgleich mit 17TRACK; liefert die Fragmente aller verfolgten Nummern."""
        now = dt_util.utcnow()
        if self._backoff_until is not None and now < self._backoff_until:
            raise SourceRateLimitError(
                f"Drosselung, naechster Versuch ab {self._backoff_until.isoformat()}"
            )
        try:
            self._next_report = {}
            shipments = await self._async_sync(now)
        except SourceRateLimitError as err:
            retry_after = getattr(err, "retry_after", None) or 0
            self._backoff = min(
                max(self._backoff * 2, SEVENTEENTRACK_BACKOFF_START, retry_after),
                SEVENTEENTRACK_BACKOFF_MAX,
            )
            self._backoff_until = now + timedelta(seconds=self._backoff)
            _LOGGER.debug("17TRACK drosselt, Pause %.0f s", self._backoff)
            raise
        self._backoff = 0.0
        self._backoff_until = None
        self._report = self._next_report
        return shipments

    def tracking_report(self) -> list[TrackingReport] | None:
        """Was der letzte Durchlauf mit jeder Nummer gemacht hat, nach Nummer sortiert."""
        if self._report is None:
            return None
        return sorted(self._report.values(), key=lambda report: report.number)

    @abstractmethod
    async def _async_sync(self, now: datetime) -> list[Shipment]:
        """Ein Durchlauf gegen den Dienst."""

    def _known_by_number(self) -> dict[str, Shipment]:
        """Der Bestand des Stores nach normalisierter Trackingnummer."""
        known: dict[str, Shipment] = {}
        for shipment in self._context.known_shipments():
            if shipment.tracking_number:
                known.setdefault(normalize_tracking_number(shipment.tracking_number), shipment)
        return known

    def _carrier_allowed(self, carrier: str) -> bool:
        """Darf diese Quelle Sendungen dieses Zustellers anfassen?"""
        return carrier not in self._excluded

    def _remote_allowed(self, number: str, code: Any, known: Mapping[str, Shipment]) -> bool:
        """Ist eine Nummer bei 17TRACK eine Sendung eines zugelassenen Zustellers?

        Fuer bekannte Sendungen zaehlt der Zusteller aus der Mail, sonst derselbe,
        den auch das Fragment bekaeme.
        """
        if not self._excluded:
            return True
        carrier, _, _ = self._identity(number, code, known.get(number))
        return self._carrier_allowed(carrier)

    # ------------------------------------------------------------------
    # Abgleich festhalten
    # ------------------------------------------------------------------

    def _note(
        self,
        number: str,
        state: TrackingState,
        now: datetime,
        *,
        shipment: Shipment | None = None,
        **fields: Any,
    ) -> None:
        """Eine Zeile des Abgleichs; Carrier und Sendung kommen aus der Sendung in HA."""
        if shipment is not None:
            fields.setdefault("carrier", shipment.carrier)
            fields.setdefault("shipment_id", shipment.id)
        self._next_report[number] = TrackingReport(
            number=number, state=state, checked_at=now, **fields
        )

    def _note_remote(
        self,
        number: str,
        code: Any,
        known: Mapping[str, Shipment],
        state: TrackingState,
        now: datetime,
        shipment: Shipment | None = None,
        **fields: Any,
    ) -> None:
        """Eine Nummer beim Dienst; ohne Sendung zaehlt der Zusteller, den er erkannt hat."""
        local = known.get(number)
        carrier, _, _ = self._identity(number, code, local)
        target = shipment or local
        if shipment is not None:
            fields.setdefault("status", shipment.status)
        self._note(
            number,
            state,
            now,
            carrier=carrier,
            shipment_id=target.id if target is not None else None,
            **fields,
        )

    def _reject(self, number: str, shipment: Shipment | None, now: datetime, detail: str) -> None:
        """Eine Ablehnung sperren, festhalten und als Event melden."""
        self._rejected[number] = now
        self._note(
            number,
            TrackingState.REJECTED,
            now,
            shipment=shipment,
            detail=detail,
            retry_at=now + timedelta(seconds=SEVENTEENTRACK_REJECT_RETRY),
        )
        if self._hass is not None:
            self._hass.bus.async_fire(
                EVENT_TRACKING_REJECTED,
                {
                    "source": self.source_id,
                    "tracking_number": number,
                    "carrier": shipment.carrier if shipment is not None else None,
                    "shipment_id": shipment.id if shipment is not None else None,
                    "reason": detail,
                },
            )

    def _registration_candidates(
        self, known: Mapping[str, Shipment], remote: Iterable[str], now: datetime
    ) -> list[tuple[str, Shipment]]:
        """Aktive Sendungen, die beim Dienst fehlen und eingetragen werden duerfen.

        Was nicht darf — Zusteller abgewaehlt, Nummer ungueltig, Ablehnung noch
        gesperrt —, landet mit Grund im Abgleich.
        """
        self._prune_rejected(now)
        present = set(remote)
        eligible: list[tuple[str, Shipment]] = []
        for number, shipment in known.items():
            if not shipment.is_active or number in present:
                continue
            if not self._carrier_allowed(shipment.carrier):
                self._note(number, TrackingState.EXCLUDED, now, shipment=shipment)
            elif not self._is_registrable(shipment, number):
                self._note(number, TrackingState.INVALID, now, shipment=shipment)
            elif (rejected_at := self._rejected.get(number)) is not None:
                self._note(
                    number,
                    TrackingState.REJECTED,
                    now,
                    shipment=shipment,
                    retry_at=rejected_at + timedelta(seconds=SEVENTEENTRACK_REJECT_RETRY),
                )
            else:
                eligible.append((number, shipment))
        return eligible

    def _prune_rejected(self, now: datetime) -> None:
        """Ablehnungen vergessen, deren Sperrfrist abgelaufen ist."""
        retry_before = now - timedelta(seconds=SEVENTEENTRACK_REJECT_RETRY)
        self._rejected = {
            number: when for number, when in self._rejected.items() if when > retry_before
        }

    def _is_registrable(self, shipment: Shipment, number: str) -> bool:
        """Besteht die Nummer die Pruefung ihres Zustellers?

        Der generische Fallback faengt bewusst weit — von ihm wird nur
        eingetragen, was eine stimmende Pruefziffer hat. Von Hand angelegte
        Nummern sind gewollt und werden ohne Pruefziffer eingetragen.
        """
        if not _MIN_NUMBER_LENGTH <= len(number) <= _MAX_NUMBER_LENGTH or not number.isalnum():
            return False
        if SOURCE_MANUAL in shipment.source_ids:
            return True
        registry = self._context.registry
        if registry is None:
            return True
        plugin = registry.get(shipment.carrier)
        if plugin is None:
            return any(result is ChecksumResult.VALID for _, result in registry.identify(number))
        result = plugin.check_tracking_number(number)
        if shipment.carrier == CARRIER_GENERIC:
            return result is ChecksumResult.VALID
        return result is not None and result is not ChecksumResult.INVALID

    def _identity(
        self, number: str, code: Any, local: Shipment | None
    ) -> tuple[str, str, str | None]:
        """Carrier, Sendungs-ID und Verfolgungslink.

        Eine bekannte Sendung behaelt ihre ID — nur so landet der Stand von
        17TRACK bei der Mail. Fuer Nummern, die nur 17TRACK kennt, entscheidet
        die Zuordnungsdatei, dann das Format der Nummer, zuletzt der Fallback.
        """
        if local is not None:
            return local.carrier, local.id, None

        carrier = self.mapping.carrier_id(code)
        registry = self._context.registry
        if carrier is None and registry is not None and (found := registry.identify(number)):
            plugin = found[0][0]
            carrier = plugin.carrier_for_number(number)
        carrier = carrier or CARRIER_GENERIC

        plugin = registry.get(carrier) if registry is not None else None
        url = plugin.tracking_url(number) if plugin is not None else None
        return (
            carrier,
            build_shipment_id(carrier, number),
            url or f"https://t.17track.net/#nums={number}",
        )


class SeventeenTrackSource(SeventeenTrackSourceBase):
    """Gleicht die aktiven Sendungen mit der 17TRACK-API ab und holt deren Status."""

    def __init__(
        self,
        client: SeventeenTrackClient,
        context: SourceContext,
        *,
        hass: HomeAssistant | None = None,
        mapping: SeventeenTrackMapping | None = None,
        source_id: str = SOURCE_SEVENTEENTRACK,
        priority: int | None = None,
        excluded_carriers: Iterable[str] = (),
    ) -> None:
        """Quelle anlegen; die Zuordnung wird ohne Vorgabe in `async_setup` geladen."""
        super().__init__(
            context,
            hass=hass,
            mapping=mapping,
            source_id=source_id,
            priority=priority,
            excluded_carriers=excluded_carriers,
        )
        self._client = client
        self._quota: Quota | None = None
        self._quota_checked: datetime | None = None
        self._quota_error: SeventeenTrackQuotaError | None = None
        self._stats = {"registered": 0, "rejected": 0, "stopped": 0, "fetched": 0}

    # ------------------------------------------------------------------
    # Zustand nach aussen
    # ------------------------------------------------------------------

    @property
    def warning(self) -> SourceError | None:
        """Aufgebrauchtes Kontingent, solange Nummern auf ihre Registrierung warten."""
        return self._quota_error

    @property
    def quota(self) -> Quota | None:
        """Zuletzt abgefragtes Kontingent."""
        return self._quota

    def diagnostics(self) -> dict[str, Any]:
        """Kontingent und Kennzahlen — ohne Schluessel und ohne Nummern."""
        return {
            "quota": self._quota.as_dict() if self._quota else None,
            "quota_checked": self._quota_checked.isoformat() if self._quota_checked else None,
            "quota_exhausted": self._quota_error is not None,
            "requests": self._client.requests,
            **self._backoff_diagnostics(),
            **self._stats,
        }

    # ------------------------------------------------------------------
    # TrackingSource
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Zuordnung laden und den Schluessel ueber das Kontingent pruefen."""
        await self._async_load_mapping()
        await self._async_update_quota(dt_util.utcnow())

    # ------------------------------------------------------------------
    # Ein Durchlauf
    # ------------------------------------------------------------------

    async def _async_sync(self, now: datetime) -> list[Shipment]:
        """Abmelden, registrieren, abfragen — in dieser Reihenfolge."""
        known = self._known_by_number()
        remote = {
            normalize_tracking_number(str(item.get("number", ""))): _int_or_none(
                item.get("carrier")
            )
            for item in await self._client.async_list_tracking()
            if item.get("number")
        }
        self._tracked = len(remote)
        # Abgewaehlte Zusteller: weder abfragen noch abmelden — als gaebe es sie nicht.
        for number, carrier in remote.items():
            if not self._remote_allowed(number, carrier, known):
                self._note_remote(number, carrier, known, TrackingState.EXCLUDED, now)
        remote = {
            number: carrier
            for number, carrier in remote.items()
            if self._remote_allowed(number, carrier, known)
        }

        # Lokal abgeschlossen (etwa per Zustellmail): nicht mehr abfragen, abmelden.
        closed = [number for number in remote if (s := known.get(number)) and s.is_closed]
        await self._async_stop(closed, remote)
        for number in closed:
            self._note_remote(number, remote[number], known, TrackingState.FINISHED, now)

        # Frisch registrierte Nummern stehen erst im naechsten Durchlauf in `remote`
        # und werden damit auch erst dann abgefragt.
        await self._async_register(self._registration_candidates(known, remote, now), now)

        query = [
            TrackItem(number, carrier) for number, carrier in remote.items() if number not in closed
        ]
        if not query:
            return []

        result = await self._client.async_get_track_info(query)
        shipments: list[Shipment] = []
        for item in result.accepted:
            number = normalize_tracking_number(str(item.get("number", "")))
            shipment = self._build_shipment(item, known, now)
            if shipment is None:
                self._note_remote(number, item.get("carrier"), known, TrackingState.NOT_FOUND, now)
                continue
            shipments.append(shipment)
            self._note_remote(
                number,
                item.get("carrier"),
                known,
                TrackingState.FINISHED if shipment.is_closed else TrackingState.TRACKING,
                now,
                shipment,
                last_event=shipment.last_updated,
            )
        for rejection in result.rejected:
            number = normalize_tracking_number(rejection.number)
            self._note_remote(
                number,
                remote.get(number),
                known,
                TrackingState.NOT_FOUND,
                now,
                detail=str(rejection.code),
            )
        self._stats["fetched"] += len(shipments)

        # Zugestellt, zurueck oder endgueltig gescheitert: kein Grund, weiter zu fragen.
        finished = [
            normalize_tracking_number(shipment.tracking_number)
            for shipment in shipments
            if shipment.is_closed and shipment.tracking_number
        ]
        await self._async_stop(finished, remote)
        return shipments

    async def _async_stop(self, numbers: list[str], remote: Mapping[str, int | None]) -> None:
        """Nummern abmelden; Ablehnungen (schon gestoppt) sind bedeutungslos."""
        if not numbers:
            return
        result = await self._client.async_stop_track(
            [TrackItem(number, remote.get(number)) for number in numbers]
        )
        self._stats["stopped"] += len(result.accepted)
        for rejection in result.rejected:
            _LOGGER.debug("17TRACK: Abmelden abgelehnt (%s)", rejection.code)

    async def _async_register(self, candidates: list[tuple[str, Shipment]], now: datetime) -> None:
        """Neue Nummern registrieren — nur gepruefte, und nur bei vorhandenem Kontingent."""
        items = [
            TrackItem(number, self.mapping.carrier_code(shipment.carrier))
            for number, shipment in candidates
        ]
        if not items:
            # Niemand wartet — ein leeres Kontingent ist dann kein Problem.
            self._quota_error = None
            return

        await self._async_update_quota(now)
        if self._quota is not None and self._quota.exhausted:
            self._set_quota_exhausted(len(items))
            self._note_pending(candidates, now)
            return

        try:
            result = await self._client.async_register(items)
        except SeventeenTrackQuotaError:
            # Die ganze Anfrage abgelehnt: Die bekannten Nummern laufen trotzdem weiter.
            self._set_quota_exhausted(len(items))
            self._note_pending(candidates, now)
            return
        self._handle_registration(result, now, dict(candidates))

    def _note_pending(self, candidates: list[tuple[str, Shipment]], now: datetime) -> None:
        """Nummern, die auf Kontingent warten."""
        for number, shipment in candidates:
            self._note(number, TrackingState.PENDING, now, shipment=shipment, detail="quota")

    def _handle_registration(
        self, result: BatchResult, now: datetime, candidates: Mapping[str, Shipment]
    ) -> None:
        """Ablehnungen sperren, ein leeres Kontingent melden, das Kontingent nachfuehren."""
        self._stats["registered"] += len(result.accepted)
        for item in result.accepted:
            number = normalize_tracking_number(str(item.get("number", "")))
            self._note(number, TrackingState.ADDED, now, shipment=candidates.get(number))

        pending = 0
        for rejection in result.rejected:
            number = normalize_tracking_number(rejection.number)
            if rejection.code == CODE_ALREADY_REGISTERED:
                # Liegt gestoppt im Konto — erneut registrieren ginge nur ueber Kontingent.
                _LOGGER.debug("17TRACK: Nummer ist bereits registriert")
            elif rejection.code == CODE_QUOTA_EXHAUSTED:
                # Kein Urteil ueber die Nummer: Sie kommt dran, sobald Kontingent da ist.
                pending += 1
                self._note(
                    number,
                    TrackingState.PENDING,
                    now,
                    shipment=candidates.get(number),
                    detail="quota",
                )
                continue
            self._reject(number, candidates.get(number), now, str(rejection.code))
            self._stats["rejected"] += 1
            _LOGGER.debug("17TRACK: Registrierung abgelehnt (%s)", rejection.code)

        if result.quota_exhausted:
            self._set_quota_exhausted(pending)
        else:
            self._quota_error = None
        if result.accepted and self._quota is not None and self._quota.remaining is not None:
            # Ohne eigene Anfrage nachfuehren: Jede Registrierung kostet eine Einheit.
            remaining = max(0, self._quota.remaining - len(result.accepted))
            used = None if self._quota.used is None else self._quota.used + len(result.accepted)
            self._quota = Quota(
                total=self._quota.total,
                used=used,
                remaining=remaining,
                today_used=self._quota.today_used,
            )

    def _set_quota_exhausted(self, waiting: int) -> None:
        """Registrieren ruht; die bekannten Nummern laufen weiter."""
        if self._quota_error is None:
            _LOGGER.warning(
                "17TRACK: Kontingent aufgebraucht, %d Nummern werden vorerst nicht registriert",
                waiting,
            )
        self._quota_error = SeventeenTrackQuotaError(
            f"Kontingent aufgebraucht, {waiting} Nummern warten auf ihre Registrierung"
        )

    async def _async_update_quota(self, now: datetime) -> None:
        """Kontingent abfragen (kostenlos)."""
        self._quota = await self._client.async_get_quota()
        self._quota_checked = now

    # ------------------------------------------------------------------
    # Fragmente bauen
    # ------------------------------------------------------------------

    def _build_shipment(
        self, item: Mapping[str, Any], known: Mapping[str, Shipment], now: datetime
    ) -> Shipment | None:
        """Ein `gettrackinfo`-Eintrag als Sendungs-Fragment.

        Ohne Ereignis und ohne Status („NotFound") entsteht nichts: Sonst
        hielte jeder Abruf eine Nummer, die der Zusteller nie kannte, kuenstlich frisch.
        """
        number = normalize_tracking_number(str(item.get("number", "")))
        if not number:
            return None
        info = item.get("track_info")
        info = info if isinstance(info, Mapping) else {}

        events = self._events(info)
        latest = info.get("latest_status") if isinstance(info.get("latest_status"), Mapping) else {}
        status = self.mapping.status(latest.get("status"), latest.get("sub_status"))
        if status is ShipmentStatus.UNKNOWN and not events:
            return None

        latest_event = info.get("latest_event")
        latest_event = latest_event if isinstance(latest_event, Mapping) else {}
        latest_time = _event_time(latest_event) or (events[-1].timestamp if events else None)

        local = known.get(number)
        carrier, shipment_id, tracking_url = self._identity(number, item.get("carrier"), local)
        eta_start, eta_end = self._eta(info)
        return Shipment(
            id=shipment_id,
            carrier=carrier,
            tracking_number=number,
            status=status,
            status_text=_text(latest_event.get("description")),
            eta_start=eta_start,
            eta_end=eta_end,
            delivered_at=latest_time if status is ShipmentStatus.DELIVERED else None,
            tracking_url=tracking_url,
            events=events,
            sources=[SourceRecord(source_id=self.source_id, fetched_at=now)],
            first_seen=min(now, events[0].timestamp) if events else now,
            last_updated=latest_time or now,
        )

    def _events(self, info: Mapping[str, Any]) -> list[ShipmentEvent]:
        """Alle Ereignisse aller Anbieter, aelteste zuerst."""
        tracking = info.get("tracking")
        providers = tracking.get("providers") if isinstance(tracking, Mapping) else None
        events: list[ShipmentEvent] = []
        for provider in providers if isinstance(providers, list) else []:
            raw_events = provider.get("events") if isinstance(provider, Mapping) else None
            for raw in raw_events if isinstance(raw_events, list) else []:
                if not isinstance(raw, Mapping) or (timestamp := _event_time(raw)) is None:
                    continue
                # `stage` ist feiner als der Hauptstatus („PickedUp", „Arrival") und
                # passt nur manchmal auf ihn — der Unterstatus traegt die Zuordnung.
                status = self.mapping.status(None, raw.get("sub_status"))
                if status is ShipmentStatus.UNKNOWN:
                    status = self.mapping.status(raw.get("stage"))
                events.append(
                    ShipmentEvent(
                        timestamp=timestamp,
                        status=status,
                        text=_text(raw.get("description")),
                        location=_location(raw.get("location")),
                    )
                )
        return sorted(events, key=lambda event: event.timestamp)

    @staticmethod
    def _eta(info: Mapping[str, Any]) -> tuple[datetime | None, datetime | None]:
        """Erwartetes Zustellfenster aus `time_metrics.estimated_delivery_date`."""
        metrics = info.get("time_metrics")
        estimate = metrics.get("estimated_delivery_date") if isinstance(metrics, Mapping) else None
        if not isinstance(estimate, Mapping):
            return None, None
        start = _parse_time(estimate.get("from"))
        end = _parse_time(estimate.get("to"))
        if start is None:
            return None, None
        if end is not None and end < start:
            end = None
        return start, end


def _int_or_none(value: Any) -> int | None:
    """Carrier-Code aus der Antwort; `0` heisst „unbekannt"."""
    try:
        return int(value) or None
    except TypeError, ValueError:
        return None


def create_source(
    hass: HomeAssistant, data: Mapping[str, Any], context: SourceContext
) -> SeventeenTrackSource:
    """Fabrik fuer `API_SOURCE_FACTORIES`."""
    client = SeventeenTrackClient(async_get_clientsession(hass), str(data[CONF_API_KEY]))
    return SeventeenTrackSource(
        client,
        context,
        hass=hass,
        excluded_carriers=data.get(CONF_EXCLUDED_CARRIERS, ()),
    )


API_SOURCE_FACTORIES[SOURCE_SEVENTEENTRACK] = create_source
