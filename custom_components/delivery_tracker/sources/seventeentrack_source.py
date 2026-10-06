"""17TRACK als Quelle: Live-Status fuer Trackingnummern aus Mails und aus dem 17TRACK-Konto.

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
from collections.abc import Mapping
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
    DOMAIN,
    SEVENTEENTRACK_BACKOFF_MAX,
    SEVENTEENTRACK_BACKOFF_START,
    SEVENTEENTRACK_REJECT_RETRY,
    SOURCE_SEVENTEENTRACK,
)
from ..merge import build_shipment_id
from ..models import Shipment, ShipmentEvent, ShipmentStatus, SourceRecord
from ..tracking import ChecksumResult, normalize_tracking_number
from .base import API_SOURCE_FACTORIES, SourceContext, SourceError, TrackingSource
from .seventeentrack_api import (
    CODE_ALREADY_REGISTERED,
    CODE_QUOTA_EXHAUSTED,
    BatchResult,
    Quota,
    SeventeenTrackClient,
    SeventeenTrackQuotaError,
    SeventeenTrackRateLimitError,
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
        return cls(carriers=carriers, statuses=statuses, sub_statuses=sub_statuses)

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


class SeventeenTrackSource(TrackingSource):
    """Gleicht die aktiven Sendungen mit 17TRACK ab und holt deren Status."""

    def __init__(
        self,
        client: SeventeenTrackClient,
        context: SourceContext,
        *,
        hass: HomeAssistant | None = None,
        mapping: SeventeenTrackMapping | None = None,
        source_id: str = SOURCE_SEVENTEENTRACK,
        priority: int | None = None,
    ) -> None:
        """Quelle anlegen; die Zuordnung wird ohne Vorgabe in `async_setup` geladen."""
        super().__init__(source_id, priority=priority)
        self._client = client
        self._context = context
        self._hass = hass
        self._mapping = mapping
        self._quota: Quota | None = None
        self._quota_checked: datetime | None = None
        self._quota_error: SeventeenTrackQuotaError | None = None
        self._backoff = 0.0
        self._backoff_until: datetime | None = None
        self._rejected: dict[str, datetime] = {}
        """Abgelehnte Nummern mit dem Zeitpunkt der Ablehnung."""
        self._tracked = 0
        self._stats = {"registered": 0, "rejected": 0, "stopped": 0, "fetched": 0}

    # ------------------------------------------------------------------
    # Zustand nach aussen
    # ------------------------------------------------------------------

    @property
    def mapping(self) -> SeventeenTrackMapping:
        """Die geladene Zuordnung."""
        if self._mapping is None:
            raise SourceError("Zuordnung von 17TRACK ist nicht geladen")
        return self._mapping

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
            "tracked": self._tracked,
            "requests": self._client.requests,
            "rejected_numbers": len(self._rejected),
            "backoff_seconds": self._backoff,
            "backoff_until": self._backoff_until.isoformat() if self._backoff_until else None,
            **self._stats,
        }

    # ------------------------------------------------------------------
    # TrackingSource
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Zuordnung laden und den Schluessel ueber das Kontingent pruefen."""
        if self._mapping is None:
            if self._hass is None:
                raise SourceError("Ohne HA-Instanz laesst sich die Zuordnung nicht laden")
            self._mapping = await async_get_mapping(self._hass)
        await self._async_update_quota(dt_util.utcnow())

    async def async_fetch(self) -> list[Shipment]:
        """Ein Abgleich mit 17TRACK; liefert die Fragmente aller verfolgten Nummern."""
        now = dt_util.utcnow()
        if self._backoff_until is not None and now < self._backoff_until:
            raise SeventeenTrackRateLimitError(
                f"Drosselung, naechster Versuch ab {self._backoff_until.isoformat()}"
            )
        try:
            shipments = await self._async_sync(now)
        except SeventeenTrackRateLimitError as err:
            self._backoff = min(
                max(self._backoff * 2, SEVENTEENTRACK_BACKOFF_START, err.retry_after or 0),
                SEVENTEENTRACK_BACKOFF_MAX,
            )
            self._backoff_until = now + timedelta(seconds=self._backoff)
            _LOGGER.debug("17TRACK drosselt, Pause %.0f s", self._backoff)
            raise
        self._backoff = 0.0
        self._backoff_until = None
        return shipments

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

        # Lokal abgeschlossen (etwa per Zustellmail): nicht mehr abfragen, abmelden.
        closed = [number for number in remote if (s := known.get(number)) and s.is_closed]
        await self._async_stop(closed, remote)

        # Frisch registrierte Nummern stehen erst im naechsten Durchlauf in `remote`
        # und werden damit auch erst dann abgefragt.
        await self._async_register(
            [
                shipment
                for number, shipment in known.items()
                if shipment.is_active and number not in remote
            ],
            now,
        )

        query = [
            TrackItem(number, carrier) for number, carrier in remote.items() if number not in closed
        ]
        if not query:
            return []

        result = await self._client.async_get_track_info(query)
        shipments = [
            shipment
            for item in result.accepted
            if (shipment := self._build_shipment(item, known, now)) is not None
        ]
        self._stats["fetched"] += len(shipments)

        # Zugestellt, zurueck oder endgueltig gescheitert: kein Grund, weiter zu fragen.
        finished = [
            normalize_tracking_number(shipment.tracking_number)
            for shipment in shipments
            if shipment.is_closed and shipment.tracking_number
        ]
        await self._async_stop(finished, remote)
        return shipments

    def _known_by_number(self) -> dict[str, Shipment]:
        """Der Bestand des Stores nach normalisierter Trackingnummer."""
        known: dict[str, Shipment] = {}
        for shipment in self._context.known_shipments():
            if shipment.tracking_number:
                known.setdefault(normalize_tracking_number(shipment.tracking_number), shipment)
        return known

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

    async def _async_register(self, candidates: list[Shipment], now: datetime) -> None:
        """Neue Nummern registrieren — nur gepruefte, und nur bei vorhandenem Kontingent."""
        retry_before = now - timedelta(seconds=SEVENTEENTRACK_REJECT_RETRY)
        self._rejected = {
            number: when for number, when in self._rejected.items() if when > retry_before
        }
        items = [
            TrackItem(number, self.mapping.carrier_code(shipment.carrier))
            for shipment in candidates
            if (number := normalize_tracking_number(shipment.tracking_number or ""))
            and number not in self._rejected
            and self._is_registrable(shipment, number)
        ]
        if not items:
            # Niemand wartet — ein leeres Kontingent ist dann kein Problem.
            self._quota_error = None
            return

        await self._async_update_quota(now)
        if self._quota is not None and self._quota.exhausted:
            self._set_quota_exhausted(len(items))
            return

        try:
            result = await self._client.async_register(items)
        except SeventeenTrackQuotaError:
            # Die ganze Anfrage abgelehnt: Die bekannten Nummern laufen trotzdem weiter.
            self._set_quota_exhausted(len(items))
            return
        self._handle_registration(result, now)

    def _handle_registration(self, result: BatchResult, now: datetime) -> None:
        """Ablehnungen sperren, ein leeres Kontingent melden, das Kontingent nachfuehren."""
        self._stats["registered"] += len(result.accepted)

        pending = 0
        for rejection in result.rejected:
            if rejection.code == CODE_ALREADY_REGISTERED:
                # Liegt gestoppt im Konto — erneut registrieren ginge nur ueber Kontingent.
                _LOGGER.debug("17TRACK: Nummer ist bereits registriert")
            elif rejection.code == CODE_QUOTA_EXHAUSTED:
                # Kein Urteil ueber die Nummer: Sie kommt dran, sobald Kontingent da ist.
                pending += 1
                continue
            self._rejected[normalize_tracking_number(rejection.number)] = now
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

    def _is_registrable(self, shipment: Shipment, number: str) -> bool:
        """Besteht die Nummer die Pruefung ihres Zustellers?

        Der generische Fallback faengt bewusst weit — von ihm wird nur
        registriert, was eine stimmende Pruefziffer hat.
        """
        if not _MIN_NUMBER_LENGTH <= len(number) <= _MAX_NUMBER_LENGTH or not number.isalnum():
            return False
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

    def _identity(
        self, number: str, code: Any, local: Shipment | None
    ) -> tuple[str, str, str | None]:
        """Carrier, Sendungs-ID und Verfolgungslink.

        Eine bekannte Sendung behaelt ihre ID — nur so landet der API-Stand bei
        der Mail. Fuer Nummern, die nur 17TRACK kennt, entscheidet die
        Zuordnungsdatei, dann das Format der Nummer, zuletzt der Fallback.
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
    return SeventeenTrackSource(client, context, hass=hass)


API_SOURCE_FACTORIES[SOURCE_SEVENTEENTRACK] = create_source
