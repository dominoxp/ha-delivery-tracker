"""17TRACK-Benutzerkonto als Quelle — kostenlos, mit E-Mail und Passwort.

Arbeitet wie die HA-Core-Integration `seventeentrack` mit dem normalen Konto
der 17TRACK-App statt mit einem API-Schluessel: kein Kontingent, keine Kosten.
Dafuer haengt sie an einer inoffiziellen Schnittstelle, und das Konto liefert
nur den letzten Stand je Sendung, keine vollstaendige Ereignis-Historie und
kein Zustellfenster.

Ein Durchlauf:

```
GetTrackInfoList ──▶ was liegt im Konto?               (je 40 gebuendelt)
   │
   ├─▶ SetTrackArchived  abgeschlossene Sendungen archivieren
   ├─▶ AddTrackNo        neue, gepruefte Nummern aus Mails (hoechstens 10 je Durchlauf)
   └─▶ Fragmente aus der Liste; zugestellte gleich archivieren
```

Neu eingetragene Nummern tauchen erst im naechsten Durchlauf in der Liste auf.
Archiviert wird wie in der App: Die Sendung bleibt im Konto sichtbar, 17TRACK
fragt sie nur nicht mehr ab.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    ISSUE_API_LOGIN_INVALID,
    SEVENTEENTRACK_ACCOUNT_MAX_ADD,
    SOURCE_SEVENTEENTRACK_ACCOUNT,
)
from ..models import Shipment, ShipmentEvent, ShipmentStatus, SourceRecord
from ..tracking import normalize_tracking_number
from .base import API_SOURCE_FACTORIES, SourceContext
from .seventeentrack_account_api import (
    CODE_OK,
    AccountPackage,
    SeventeenTrackAccountClient,
)
from .seventeentrack_source import SeventeenTrackMapping, SeventeenTrackSourceBase

_LOGGER = logging.getLogger(__name__)

_TIME_FORMATS: Final = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def parse_account_time(value: str | None) -> datetime | None:
    """Zeit des letzten Ereignisses; ohne Zeitzone geliefert, gilt als Ortszeit von HA."""
    if not value:
        return None
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return dt_util.as_utc(parsed.replace(tzinfo=dt_util.get_default_time_zone()))
    parsed_iso = dt_util.parse_datetime(value)
    if parsed_iso is None:
        return None
    if parsed_iso.tzinfo is None:
        parsed_iso = parsed_iso.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(parsed_iso)


class SeventeenTrackAccountSource(SeventeenTrackSourceBase):
    """Gleicht die aktiven Sendungen mit dem 17TRACK-Benutzerkonto ab."""

    auth_issue = ISSUE_API_LOGIN_INVALID

    def __init__(
        self,
        client: SeventeenTrackAccountClient,
        context: SourceContext,
        *,
        hass: HomeAssistant | None = None,
        mapping: SeventeenTrackMapping | None = None,
        source_id: str = SOURCE_SEVENTEENTRACK_ACCOUNT,
        priority: int | None = None,
    ) -> None:
        """Quelle anlegen; die Zuordnung wird ohne Vorgabe in `async_setup` geladen."""
        super().__init__(
            context, hass=hass, mapping=mapping, source_id=source_id, priority=priority
        )
        self._client = client
        self._stats = {"added": 0, "rejected": 0, "archived": 0, "fetched": 0}

    def diagnostics(self) -> dict[str, Any]:
        """Kennzahlen — ohne Zugangsdaten und ohne Nummern."""
        return {
            "logged_in": self._client.logged_in,
            "logins": self._client.logins,
            "requests": self._client.requests,
            **self._backoff_diagnostics(),
            **self._stats,
        }

    # ------------------------------------------------------------------
    # TrackingSource
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Zuordnung laden und anmelden — falsche Zugangsdaten fallen hier auf."""
        await self._async_load_mapping()
        await self._client.async_login()

    async def async_close(self) -> None:
        """Die Session mit den Cookies der Anmeldung schliessen."""
        await self._client.async_close()

    # ------------------------------------------------------------------
    # Ein Durchlauf
    # ------------------------------------------------------------------

    async def _async_sync(self, now: datetime) -> list[Shipment]:
        """Archivieren, eintragen, Fragmente bauen — in dieser Reihenfolge."""
        known = self._known_by_number()
        remote: dict[str, AccountPackage] = {}
        for package in await self._client.async_list_packages():
            if number := normalize_tracking_number(package.number):
                remote.setdefault(number, package)
        self._tracked = len(remote)

        # Lokal abgeschlossen (etwa per Zustellmail): nicht mehr abfragen lassen.
        closed = [number for number in remote if (s := known.get(number)) and s.is_closed]
        await self._async_archive(closed, remote)

        await self._async_add(
            [
                shipment
                for number, shipment in known.items()
                if shipment.is_active and number not in remote
            ],
            now,
        )

        shipments = [
            shipment
            for number, package in remote.items()
            if number not in closed
            and (shipment := self._build_shipment(number, package, known, now)) is not None
        ]
        self._stats["fetched"] += len(shipments)

        # Zugestellt, zurueck oder endgueltig gescheitert: archivieren wie in der App.
        finished = [
            normalize_tracking_number(shipment.tracking_number)
            for shipment in shipments
            if shipment.is_closed and shipment.tracking_number
        ]
        await self._async_archive(finished, remote)
        return shipments

    async def _async_archive(
        self, numbers: list[str], remote: Mapping[str, AccountPackage]
    ) -> None:
        """Sendungen archivieren; ohne interne ID geht das nicht und wird uebersprungen."""
        ids = [
            package.internal_id
            for number in numbers
            if (package := remote.get(number)) is not None and package.internal_id
        ]
        if not ids:
            return
        if await self._client.async_archive(ids):
            self._stats["archived"] += len(ids)
        else:
            _LOGGER.debug("17TRACK-Konto: Archivieren abgelehnt")

    async def _async_add(self, candidates: list[Shipment], now: datetime) -> None:
        """Neue, gepruefte Nummern eintragen — eine Anfrage je Nummer, gedeckelt."""
        self._prune_rejected(now)
        numbers = [
            number
            for shipment in candidates
            if (number := normalize_tracking_number(shipment.tracking_number or ""))
            and number not in self._rejected
            and self._is_registrable(shipment, number)
        ]
        for number in numbers[:SEVENTEENTRACK_ACCOUNT_MAX_ADD]:
            code = await self._client.async_add(number)
            if code == CODE_OK:
                self._stats["added"] += 1
                continue
            # Ungueltige Nummer oder volles Konto — erst nach der Sperrfrist erneut versuchen.
            self._rejected[number] = now
            self._stats["rejected"] += 1
            _LOGGER.debug("17TRACK-Konto: Eintragen abgelehnt (%s)", code)

    # ------------------------------------------------------------------
    # Fragmente bauen
    # ------------------------------------------------------------------

    def _build_shipment(
        self,
        number: str,
        package: AccountPackage,
        known: Mapping[str, Shipment],
        now: datetime,
    ) -> Shipment | None:
        """Ein Listeneintrag als Sendungs-Fragment.

        Ohne Ereignis und ohne Status („Not Found") entsteht nichts: Sonst
        hielte jeder Abruf eine Nummer, die der Zusteller nie kannte, kuenstlich frisch.
        """
        status = self.mapping.account_status(package.state)
        event_time = parse_account_time(package.event_time)
        events = (
            [
                ShipmentEvent(
                    timestamp=event_time,
                    status=status,
                    text=package.event_text,
                    location=package.event_location,
                )
            ]
            if event_time is not None
            else []
        )
        if status is ShipmentStatus.UNKNOWN and not events:
            return None

        local = known.get(number)
        carrier, shipment_id, tracking_url = self._identity(number, package.carrier, local)
        return Shipment(
            id=shipment_id,
            carrier=carrier,
            tracking_number=number,
            status=status,
            status_text=package.event_text,
            delivered_at=event_time if status is ShipmentStatus.DELIVERED else None,
            tracking_url=tracking_url,
            # Der Name aus der App hilft nur Sendungen, die keine Mail beschreibt.
            description=package.remark if local is None else None,
            events=events,
            sources=[SourceRecord(source_id=self.source_id, fetched_at=now)],
            first_seen=min(now, event_time) if event_time else now,
            last_updated=event_time or now,
        )


def create_source(
    hass: HomeAssistant, data: Mapping[str, Any], context: SourceContext
) -> SeventeenTrackAccountSource:
    """Fabrik fuer `API_SOURCE_FACTORIES` — mit eigener Session fuer die Cookies."""
    client = SeventeenTrackAccountClient(
        async_create_clientsession(hass),
        str(data[CONF_USERNAME]),
        str(data[CONF_PASSWORD]),
    )
    return SeventeenTrackAccountSource(client, context, hass=hass)


API_SOURCE_FACTORIES[SOURCE_SEVENTEENTRACK_ACCOUNT] = create_source
