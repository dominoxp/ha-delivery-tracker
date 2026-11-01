"""Duenner Client fuer das 17TRACK-Benutzerkonto (kostenlos, inoffiziell).

Spricht dieselben Endpunkte wie die Website und die HA-Core-Integration
`seventeentrack`: Anmeldung mit E-Mail und Passwort bei `user.17track.net`,
danach Aufrufe an `buyer.17track.net` mit den Cookies der Anmeldung. Das
Passwort geht nur RSA-verschluesselt mit dem oeffentlichen Schluessel der
Website hinaus.

Die Schnittstelle ist nicht dokumentiert und kann sich ohne Vorwarnung
aendern — dafuer kostet sie nichts und kennt kein Kontingent. Die
kostenpflichtige, offizielle API spricht `seventeentrack_api`.

Zwei Fehlerbilder muessen sich unterscheiden lassen:

* **Anmeldung abgelehnt** (`SeventeenTrackAccountAuthError`) — dauerhaft, Reauth.
* **Drosselung** (`SeventeenTrackAccountRateLimitError`, HTTP 429) — voruebergehend.

Alles andere gilt als voruebergehend. Zugangsdaten stehen in keinem Log und in
keiner Fehlermeldung.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from yarl import URL

from ..const import (
    SEVENTEENTRACK_ACCOUNT_LOGIN_URL,
    SEVENTEENTRACK_ACCOUNT_PAGE_SIZE,
    SEVENTEENTRACK_ACCOUNT_REQUEST_INTERVAL,
    SEVENTEENTRACK_ACCOUNT_URL,
    SEVENTEENTRACK_MAX_PAGES,
    SEVENTEENTRACK_REQUEST_TIMEOUT,
)
from .base import SourceAuthError, SourceError, SourceRateLimitError

_LOGGER = logging.getLogger(__name__)

CODE_OK: Final = 0

# Oeffentlicher Schluessel, mit dem die Website das Passwort vor dem Senden verschluesselt.
_PUBLIC_KEY_PEM: Final = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0Y5iQN3VNofXPtZXYZe9
75ojD+Gb+yPBrlrKj2t4XvYHE+pYmFzGPTvDmB3t1OfHujdgBVc3VJBSFsHezm4kz
4iqIChHLKeFvuux+i/Uq+zo1QdC72qteUMHF925qPLe3xU/QJj6BFR9mA4VrUwXt8
eWI58ozizBH31PclxiPNT+yYYXRUV3QJvbZ+FJGL3gYUu1k44WILQzDZfBJMRf+My
LHZew6+XtYB8E2+PXc/R7TtLPcMDsPvARrAJMhu5b+yfwJM1zOFChAz3U1w0Zkj1y
VJQaa/aktmfd0KyFkU2M0xNpPIIcQkywUGCMZEmJkEIyyV/I+H/NvQ+qqU5llwIDAQAB
-----END PUBLIC KEY-----
"""


class SeventeenTrackAccountError(SourceError):
    """Basisfehler des Benutzerkontos — ohne Unterklasse voruebergehend."""


class SeventeenTrackAccountAuthError(SeventeenTrackAccountError, SourceAuthError):
    """E-Mail oder Passwort werden abgelehnt."""


class SeventeenTrackAccountRateLimitError(SeventeenTrackAccountError, SourceRateLimitError):
    """Zu viele Anfragen; `retry_after` ist der Vorschlag des Servers in Sekunden."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Fehler mit optionaler Wartezeit."""
        super().__init__(message)
        self.retry_after = retry_after


def encrypt_password(password: str) -> str:
    """Das Passwort so verschluesseln, wie es die Website vor der Anmeldung tut."""
    key = serialization.load_pem_public_key(_PUBLIC_KEY_PEM)
    if not isinstance(key, rsa.RSAPublicKey):
        raise TypeError("Der oeffentliche Schluessel von 17TRACK ist kein RSA-Schluessel")
    return base64.b64encode(key.encrypt(password.encode(), padding.PKCS1v15())).decode()


@dataclass(frozen=True, slots=True)
class AccountPackage:
    """Eine Sendung aus der Liste des Benutzerkontos."""

    number: str
    internal_id: str | None = None
    """`FTrackInfoId` — braucht 17TRACK zum Archivieren, nicht die Nummer."""
    state: int = 0
    """`FPackageState`; die Zuordnung steht unter `account_statuses` in `seventeentrack.yaml`."""
    carrier: int | None = None
    remark: str | None = None
    """Der Name, den der Nutzer der Sendung in der App gegeben hat."""
    event_time: str | None = None
    """Zeit des letzten Ereignisses, so wie 17TRACK sie liefert (ohne Zeitzone)."""
    event_text: str | None = None
    event_location: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AccountPackage | None:
        """Ein Eintrag aus `GetTrackInfoList`; ohne Nummer `None`."""
        number = str(row.get("FTrackNo") or "").strip()
        if not number:
            return None
        event: Any = {}
        raw_event = row.get("FLastEvent")
        if isinstance(raw_event, str) and raw_event:
            try:
                event = json.loads(raw_event)
            except ValueError:
                event = {}
        if not isinstance(event, Mapping):
            event = {}
        location = " ".join(str(event[key]).strip() for key in ("c", "d") if event.get(key)).strip()
        internal_id = row.get("FTrackInfoId")
        return cls(
            number=number,
            internal_id=str(internal_id) if internal_id else None,
            state=_int(row.get("FPackageState")) or 0,
            carrier=_int(row.get("FFirstCarrier")) or None,
            remark=_str(row.get("FRemark")),
            event_time=_str(event.get("a")),
            event_text=_str(event.get("z")),
            event_location=location or None,
        )


def _int(value: Any) -> int | None:
    """Zahl aus der Antwort lesen; alles Unlesbare wird `None`."""
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _str(value: Any) -> str | None:
    """Text aus der Antwort; leere Werte werden `None`."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


class SeventeenTrackAccountClient:
    """Zugriff auf das 17TRACK-Benutzerkonto ueber eine eigene Session.

    Die Session gehoert dem Client: Sie traegt die Cookies der Anmeldung und
    darf deshalb nicht mit anderen Integrationen geteilt werden.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        login_url: str = SEVENTEENTRACK_ACCOUNT_LOGIN_URL,
        url: str = SEVENTEENTRACK_ACCOUNT_URL,
        request_interval: float | None = None,
        timeout: float = SEVENTEENTRACK_REQUEST_TIMEOUT,
    ) -> None:
        """Client anlegen; es wird noch nichts angefragt."""
        self._session = session
        self._username = username
        self._password = password
        self._login_url = login_url
        self._url = url
        self._request_interval = (
            SEVENTEENTRACK_ACCOUNT_REQUEST_INTERVAL
            if request_interval is None
            else request_interval
        )
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._logged_in = False
        self.requests = 0
        """Anfragen seit dem Anlegen — fuer die Diagnostics."""
        self.logins = 0

    def __repr__(self) -> str:
        """Ohne Zugangsdaten."""
        return f"<SeventeenTrackAccountClient {URL(self._url).host}>"

    @property
    def logged_in(self) -> bool:
        """True nach einer erfolgreichen Anmeldung."""
        return self._logged_in

    async def async_close(self) -> None:
        """Die eigene Session schliessen. Mehrfach aufrufbar."""
        self._logged_in = False
        if not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Endpunkte
    # ------------------------------------------------------------------

    async def async_login(self) -> None:
        """Anmelden; wirft `SeventeenTrackAccountAuthError` bei falschen Zugangsdaten."""
        self._logged_in = False
        body = await self._async_post(
            self._login_url,
            {
                "source": 0,
                "account": self._username,
                "password": encrypt_password(self._password),
            },
            what="Anmeldung",
        )
        data = body.get("data")
        if _int(body.get("code")) not in (None, CODE_OK) or not (
            isinstance(data, Mapping) and data.get("gid")
        ):
            raise SeventeenTrackAccountAuthError("Anmeldung bei 17TRACK abgelehnt")
        self._copy_cookies()
        self._logged_in = True
        self.logins += 1

    async def async_list_packages(self) -> list[AccountPackage]:
        """Alle nicht archivierten Sendungen des Kontos, seitenweise eingesammelt."""
        packages: list[AccountPackage] = []
        seen: set[str] = set()
        for page in range(1, SEVENTEENTRACK_MAX_PAGES + 1):
            body = await self._async_call(
                "GetTrackInfoList",
                {
                    "IsArchived": False,
                    "Item": "",
                    "Page": page,
                    "PerPage": SEVENTEENTRACK_ACCOUNT_PAGE_SIZE,
                    "PackageState": "",
                    "Sequence": "0",
                },
                relogin=True,
            )
            rows = body.get("Json")
            rows = (
                [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
            )
            fresh = [
                package
                for row in rows
                if (package := AccountPackage.from_row(row)) is not None
                and package.number not in seen
            ]
            if not fresh:
                # Leere Seite oder dieselbe Seite noch einmal: Ende der Liste.
                break
            seen.update(package.number for package in fresh)
            packages.extend(fresh)
            page_info = body.get("pageInfo")
            total = _int(page_info.get("TotalCount")) if isinstance(page_info, Mapping) else None
            if len(rows) < SEVENTEENTRACK_ACCOUNT_PAGE_SIZE or (
                total is not None and len(packages) >= total
            ):
                break
        return packages

    async def async_add(self, number: str) -> int:
        """Eine Nummer ins Konto eintragen; liefert den `Code` der Antwort (0 = angenommen)."""
        body = await self._async_call("AddTrackNo", {"TrackNos": [number]})
        return _int(body.get("Code")) or CODE_OK

    async def async_archive(self, internal_ids: Sequence[str]) -> bool:
        """Sendungen archivieren — 17TRACK fragt sie dann nicht mehr ab."""
        if not internal_ids:
            return True
        body = await self._async_call("SetTrackArchived", {"TrackInfoIds": list(internal_ids)})
        return (_int(body.get("Code")) or CODE_OK) == CODE_OK

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _async_call(
        self, method: str, param: Mapping[str, Any], *, relogin: bool = False
    ) -> dict[str, Any]:
        """Ein Aufruf an die Konto-Schnittstelle, bei Bedarf vorher angemeldet.

        Mit `relogin` wird eine abgelaufene Anmeldung (Antwort mit `Code` != 0)
        einmal erneuert und der Aufruf wiederholt; scheitert er danach noch,
        ist das ein voruebergehender Fehler. Ohne `relogin` entscheidet der
        Aufrufer ueber den `Code` — beim Eintragen heisst er meist „Nummer
        abgelehnt", nicht „abgemeldet".
        """
        if not self._logged_in:
            await self.async_login()
        payload = {"version": "1.0", "method": method, "param": dict(param), "sourcetype": 0}
        body = await self._async_post(self._url, payload, what=method)
        if not relogin or (_int(body.get("Code")) or CODE_OK) == CODE_OK:
            return body

        _LOGGER.debug("17TRACK-Konto: %s abgelehnt (%s), neue Anmeldung", method, body.get("Code"))
        await self.async_login()
        body = await self._async_post(self._url, payload, what=method)
        code = _int(body.get("Code")) or CODE_OK
        if code != CODE_OK:
            raise SeventeenTrackAccountError(f"{method}: abgelehnt ({code})")
        return body

    def _copy_cookies(self) -> None:
        """Die Cookies der Anmeldung auch fuer die Konto-Schnittstelle setzen.

        `user.17track.net` setzt sie teils ohne `Domain`; dann schickt die
        Session sie nur an genau diesen Host zurueck, nicht an `buyer.17track.net`.
        """
        jar = self._session.cookie_jar
        cookies = jar.filter_cookies(URL(self._login_url))
        if cookies:
            jar.update_cookies(cookies, URL(self._url))

    async def _async_post(self, url: str, payload: Any, *, what: str) -> dict[str, Any]:
        """Einen POST absetzen, mit Mindestabstand, und HTTP-Fehler einordnen."""
        async with self._lock:
            wait = self._last_request + self._request_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            self.requests += 1
            try:
                async with (
                    asyncio.timeout(self._timeout),
                    self._session.post(url, json=payload) as response,
                ):
                    if (
                        response.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
                        and url != self._login_url
                    ):
                        # Ausserhalb der Anmeldung heisst das: Sitzung abgelaufen.
                        return {"Code": response.status}
                    _check_http_status(what, response)
                    body = await response.json(content_type=None)
            except SeventeenTrackAccountError:
                raise
            except TimeoutError as err:
                raise SeventeenTrackAccountError(f"{what}: keine Antwort") from err
            except (aiohttp.ClientError, ValueError) as err:
                raise SeventeenTrackAccountError(f"{what}: {type(err).__name__}") from err
        if not isinstance(body, dict):
            raise SeventeenTrackAccountError(f"{what}: unerwartete Antwort")
        return body


def _check_http_status(what: str, response: aiohttp.ClientResponse) -> None:
    """HTTP-Fehler einordnen; Ablehnungen kommen meist mit HTTP 200 und eigenem Code."""
    if response.status == HTTPStatus.TOO_MANY_REQUESTS:
        retry_after: float | None = None
        try:
            retry_after = max(0.0, float(response.headers.get("Retry-After", "")))
        except ValueError:
            retry_after = None
        raise SeventeenTrackAccountRateLimitError(
            f"{what}: Anfragen werden gedrosselt", retry_after
        )
    if response.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        raise SeventeenTrackAccountAuthError(f"{what}: Zugriff verweigert ({response.status})")
    if response.status >= HTTPStatus.BAD_REQUEST:
        raise SeventeenTrackAccountError(f"{what}: HTTP {response.status}")
