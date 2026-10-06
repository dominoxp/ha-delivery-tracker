"""Duenner Client fuer die 17TRACK-API (v2.4).

Kennt nur Transport und Fehlerbilder, keine Sendungen: Was aus den Antworten
wird, entscheidet `seventeentrack_source`. Jede Anfrage ist ein POST mit dem
Schluessel im Header `17token`; die Antwort traegt einen eigenen `code`, der
auch bei HTTP 200 ein Fehler sein kann.

Drei Fehlerbilder muessen sich unterscheiden lassen, weil sie verschieden
behandelt werden:

* **Schluessel abgelehnt** (`SeventeenTrackAuthError`) — dauerhaft, Reauth.
* **Kontingent aufgebraucht** (`SeventeenTrackQuotaError` bzw. eine Ablehnung
  mit `CODE_QUOTA_EXHAUSTED`) — betrifft nur das Registrieren.
* **Drosselung** (`SeventeenTrackRateLimitError`, HTTP 429) — voruebergehend.

Der Schluessel steht in keinem Log und in keiner Fehlermeldung.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Final

import aiohttp

from ..const import (
    SEVENTEENTRACK_API_URL,
    SEVENTEENTRACK_BATCH_SIZE,
    SEVENTEENTRACK_MAX_PAGES,
    SEVENTEENTRACK_REQUEST_INTERVAL,
    SEVENTEENTRACK_REQUEST_TIMEOUT,
)
from .base import SourceAuthError, SourceError, SourceQuotaError, SourceRateLimitError

_LOGGER = logging.getLogger(__name__)

# Fehlercodes der API, soweit sie hier eine Rolle spielen.
CODE_OK: Final = 0
CODE_IP_NOT_WHITELISTED: Final = -18010001
CODE_INVALID_KEY: Final = -18010002
CODE_ACCOUNT_DISABLED: Final = -18010004
CODE_UNAUTHORIZED: Final = -18010005
CODE_ALREADY_REGISTERED: Final = -18019901
CODE_NOT_REGISTERED: Final = -18019902
CODE_CARRIER_NOT_DETECTED: Final = -18019903
CODE_QUOTA_EXHAUSTED: Final = -18019908

# Alles, was nur ein anderer Schluessel oder eine Einstellung im 17TRACK-Konto behebt.
AUTH_CODES: Final = frozenset(
    {CODE_IP_NOT_WHITELISTED, CODE_INVALID_KEY, CODE_ACCOUNT_DISABLED, CODE_UNAUTHORIZED}
)

# Nur Nummern, die 17TRACK gerade aktiv verfolgt (nicht gestoppt).
TRACKING_STATUS_ACTIVE: Final = "Tracking"


class SeventeenTrackError(SourceError):
    """Basisfehler der 17TRACK-API — ohne Unterklasse voruebergehend."""


class SeventeenTrackAuthError(SeventeenTrackError, SourceAuthError):
    """Der API-Schluessel wird abgelehnt."""


class SeventeenTrackQuotaError(SeventeenTrackError, SourceQuotaError):
    """Das Kontingent fuer neue Registrierungen ist aufgebraucht."""


class SeventeenTrackRateLimitError(SeventeenTrackError, SourceRateLimitError):
    """Zu viele Anfragen; `retry_after` ist der Vorschlag des Servers in Sekunden."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Fehler mit optionaler Wartezeit."""
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class TrackItem:
    """Eine Nummer, optional mit dem 17TRACK-Code ihres Zustellers."""

    number: str
    carrier: int | None = None

    def as_payload(self) -> dict[str, Any]:
        """Eintrag im Format der API; ohne Code erkennt 17TRACK den Zusteller selbst."""
        if self.carrier is None:
            return {"number": self.number}
        return {"number": self.number, "carrier": self.carrier}


@dataclass(frozen=True, slots=True)
class Rejection:
    """Eine Nummer, die die API abgelehnt hat."""

    number: str
    code: int
    message: str = ""


@dataclass(slots=True)
class BatchResult:
    """Angenommene Eintraege (roh) und Ablehnungen einer Sammelanfrage."""

    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)

    @property
    def quota_exhausted(self) -> bool:
        """True, wenn mindestens eine Ablehnung am Kontingent lag."""
        return any(item.code == CODE_QUOTA_EXHAUSTED for item in self.rejected)


@dataclass(frozen=True, slots=True)
class Quota:
    """Kontingent des Schluessels laut `getquota`."""

    total: int | None = None
    used: int | None = None
    remaining: int | None = None
    today_used: int | None = None

    @property
    def exhausted(self) -> bool:
        """True, wenn keine Registrierung mehr moeglich ist."""
        return self.remaining is not None and self.remaining <= 0

    def as_dict(self) -> dict[str, Any]:
        """Fuer die Diagnostics."""
        return {
            "total": self.total,
            "used": self.used,
            "remaining": self.remaining,
            "today_used": self.today_used,
        }


def _int(value: Any) -> int | None:
    """Zahl aus der Antwort lesen; alles Unlesbare wird `None`."""
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _chunks[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    """Eine Liste in Bloecke der erlaubten Groesse teilen."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


class SeventeenTrackClient:
    """Zugriff auf die 17TRACK-API ueber die Session von HA."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        base_url: str = SEVENTEENTRACK_API_URL,
        request_interval: float | None = None,
        timeout: float = SEVENTEENTRACK_REQUEST_TIMEOUT,
    ) -> None:
        """Client anlegen; es wird noch nichts angefragt."""
        self._session = session
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._request_interval = (
            SEVENTEENTRACK_REQUEST_INTERVAL if request_interval is None else request_interval
        )
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self.requests = 0
        """Anfragen seit dem Anlegen — fuer die Diagnostics."""

    def __repr__(self) -> str:
        """Ohne Schluessel."""
        return f"<SeventeenTrackClient {self._base_url}>"

    # ------------------------------------------------------------------
    # Endpunkte
    # ------------------------------------------------------------------

    async def async_get_quota(self) -> Quota:
        """Kontingent abfragen — kostet selbst nichts und prueft nebenbei den Schluessel."""
        data = await self._async_request("getquota", [])
        return Quota(
            total=_int(data.get("quota_total")),
            used=_int(data.get("quota_used")),
            remaining=_int(data.get("quota_remain")),
            today_used=_int(data.get("today_used")),
        )

    async def async_register(self, items: Sequence[TrackItem]) -> BatchResult:
        """Nummern zur Verfolgung anmelden. Das ist der Aufruf, der Kontingent kostet."""
        return await self._async_batch("register", items)

    async def async_get_track_info(self, items: Sequence[TrackItem]) -> BatchResult:
        """Den aktuellen Stand registrierter Nummern holen."""
        return await self._async_batch("gettrackinfo", items)

    async def async_stop_track(self, items: Sequence[TrackItem]) -> BatchResult:
        """Die Verfolgung beenden — 17TRACK fragt den Zusteller dann nicht mehr ab."""
        return await self._async_batch("stoptrack", items)

    async def async_list_tracking(self) -> list[dict[str, Any]]:
        """Alle Nummern, die 17TRACK gerade verfolgt, seitenweise eingesammelt."""
        found: list[dict[str, Any]] = []
        page = 1
        while page <= SEVENTEENTRACK_MAX_PAGES:
            data, meta = await self._async_request_with_page(
                "gettracklist", {"tracking_status": TRACKING_STATUS_ACTIVE, "page_no": page}
            )
            found.extend(item for item in data.get("accepted") or [] if isinstance(item, dict))
            total = _int(meta.get("page_total")) or 1
            if page >= total:
                break
            page += 1
        return found

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _async_batch(self, path: str, items: Sequence[TrackItem]) -> BatchResult:
        """Eine Sammelanfrage in Bloecken zu hoechstens 40 Nummern."""
        result = BatchResult()
        for chunk in _chunks(list(items), SEVENTEENTRACK_BATCH_SIZE):
            data = await self._async_request(path, [item.as_payload() for item in chunk])
            result.accepted.extend(
                item for item in data.get("accepted") or [] if isinstance(item, dict)
            )
            for item in data.get("rejected") or []:
                if not isinstance(item, dict):
                    continue
                error = item.get("error") or {}
                result.rejected.append(
                    Rejection(
                        number=str(item.get("number", "")),
                        code=_int(error.get("code")) or 0,
                        message=str(error.get("message") or ""),
                    )
                )
        return result

    async def _async_request(self, path: str, payload: Any) -> dict[str, Any]:
        """Eine Anfrage; liefert den `data`-Teil der Antwort."""
        data, _ = await self._async_request_with_page(path, payload)
        return data

    async def _async_request_with_page(
        self, path: str, payload: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Eine Anfrage; liefert `data` und den Seitenteil (`page`) der Antwort."""
        async with self._lock:
            await self._async_throttle()
            body = await self._async_post(path, payload)

        code = _int(body.get("code"))
        if code != CODE_OK:
            data = body.get("data")
            errors = data.get("errors") if isinstance(data, dict) else None
            message = ""
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = str(errors[0].get("message") or "")
                code = _int(errors[0].get("code")) or code
            raise _error_for_code(path, code, message)

        data = body.get("data")
        page = body.get("page")
        return (
            data if isinstance(data, dict) else {},
            page if isinstance(page, dict) else {},
        )

    async def _async_throttle(self) -> None:
        """Den Mindestabstand zwischen zwei Anfragen einhalten (3 je Sekunde)."""
        wait = self._last_request + self._request_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    async def _async_post(self, path: str, payload: Any) -> dict[str, Any]:
        """Den POST absetzen und HTTP-Fehler einordnen."""
        self.requests += 1
        try:
            async with (
                asyncio.timeout(self._timeout),
                self._session.post(
                    f"{self._base_url}/{path}",
                    json=payload,
                    headers={"17token": self._api_key, "Content-Type": "application/json"},
                ) as response,
            ):
                _check_http_status(path, response)
                body = await response.json(content_type=None)
        except SeventeenTrackError:
            raise
        except TimeoutError as err:
            raise SeventeenTrackError(f"{path}: keine Antwort") from err
        except (aiohttp.ClientError, ValueError) as err:
            raise SeventeenTrackError(f"{path}: {type(err).__name__}") from err
        if not isinstance(body, dict):
            raise SeventeenTrackError(f"{path}: unerwartete Antwort")
        return body


def _check_http_status(path: str, response: aiohttp.ClientResponse) -> None:
    """HTTP-Fehler einordnen; die meisten Fehler meldet die API aber ueber `code`."""
    if response.status == HTTPStatus.TOO_MANY_REQUESTS:
        raise SeventeenTrackRateLimitError(
            f"{path}: Anfragen werden gedrosselt",
            _retry_after(response.headers.get("Retry-After")),
        )
    if response.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        raise SeventeenTrackAuthError(f"{path}: Zugriff verweigert ({response.status})")
    if response.status >= HTTPStatus.BAD_REQUEST:
        raise SeventeenTrackError(f"{path}: HTTP {response.status}")


def _error_for_code(path: str, code: int | None, message: str) -> SeventeenTrackError:
    """Den passenden Fehler zu einem `code` der API."""
    text = f"{path}: {message or 'Fehler'} ({code})"
    if code in AUTH_CODES:
        return SeventeenTrackAuthError(text)
    if code == CODE_QUOTA_EXHAUSTED:
        return SeventeenTrackQuotaError(text)
    return SeventeenTrackError(text)


def _retry_after(value: str | None) -> float | None:
    """`Retry-After` in Sekunden; ein HTTP-Datum wird ignoriert."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
