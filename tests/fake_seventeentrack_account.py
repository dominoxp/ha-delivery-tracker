"""Fake des 17TRACK-Benutzerkontos fuer die Tests.

Haengt sich in `aioclient_mock` und spielt Anmeldung und Konto-Schnittstelle
mit eigenem Zustand nach: Sendungen im Konto, archivierte Sendungen, eine
Sitzung, die ablaufen kann, und Drosselung. Das Passwort kommt wie beim
echten Dienst RSA-verschluesselt an; das Fake bringt dafuer ein eigenes
Schluesselpaar mit, dessen oeffentlicher Teil im Client eingesetzt wird.
"""

from __future__ import annotations

import base64
import json
from http import HTTPStatus
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.delivery_tracker.const import (
    SEVENTEENTRACK_ACCOUNT_LOGIN_URL,
    SEVENTEENTRACK_ACCOUNT_URL,
)

ACCOUNT_USERNAME = "paket@example.com"
ACCOUNT_PASSWORD = "geheim 123"

# Schluesselpaar fuer alle Tests: Erzeugen kostet Zeit, und geheim ist hier nichts.
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=1024)
PUBLIC_KEY_PEM = _PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)

_PAGE_SIZE = 40
CODE_NOT_LOGGED_IN = -11
CODE_INVALID_NUMBER = -5


def last_event(time: str, text: str, city: str = "", country: str = "") -> str:
    """`FLastEvent` so, wie das Konto es liefert: JSON als Text."""
    return json.dumps({"a": time, "z": text, "c": city, "d": country})


class FakeSeventeenTrackAccount:
    """Zustand und Antworten des Fakes."""

    def __init__(
        self, *, username: str = ACCOUNT_USERNAME, password: str = ACCOUNT_PASSWORD
    ) -> None:
        """Leeres Konto, noch niemand angemeldet."""
        self.username = username
        self.password = password
        self.packages: dict[str, dict[str, Any]] = {}
        """Nicht archivierte Sendungen nach Nummer, als Zeilen von `GetTrackInfoList`."""
        self.archived: dict[str, dict[str, Any]] = {}
        self.invalid: set[str] = set()
        """Nummern, die `AddTrackNo` ablehnt."""
        self.session = False
        self.logins = 0
        self.throttle = 0
        """So viele der naechsten Anfragen werden mit HTTP 429 beantwortet."""
        self.broken = False
        """True: Jede Anfrage scheitert mit HTTP 500."""
        self.calls: list[tuple[str, Any]] = []
        self._next_id = 1

    def install(self, mocker: AiohttpClientMocker) -> None:
        """Beide Endpunkte im Mocker anmelden."""
        mocker.post(SEVENTEENTRACK_ACCOUNT_LOGIN_URL, side_effect=self._handle)
        mocker.post(SEVENTEENTRACK_ACCOUNT_URL, side_effect=self._handle)

    def add(
        self,
        number: str,
        *,
        state: int = 10,
        carrier: int = 0,
        remark: str | None = None,
        event: str | None = None,
    ) -> dict[str, Any]:
        """Eine Sendung liegt schon im Konto (ohne Zutun der Quelle)."""
        row = {
            "FTrackNo": number,
            "FTrackInfoId": f"id-{self._next_id}",
            "FPackageState": state,
            "FFirstCarrier": carrier,
            "FRemark": remark,
            "FLastEvent": event,
        }
        self._next_id += 1
        self.packages[number] = row
        return row

    def update(self, number: str, **fields: Any) -> None:
        """Den Stand einer Sendung im Konto aendern."""
        self.packages[number].update(fields)

    def methods(self) -> list[str]:
        """Aufgerufene Methoden (`login` fuer die Anmeldung) in Reihenfolge."""
        return [method for method, _ in self.calls]

    def params(self, method: str) -> list[Any]:
        """Alle Parameter einer Methode."""
        return [param for name, param in self.calls if name == method]

    # ------------------------------------------------------------------

    async def _handle(self, method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        is_login = str(url) == SEVENTEENTRACK_ACCOUNT_LOGIN_URL
        name = "login" if is_login else str(data.get("method"))
        self.calls.append((name, None if is_login else data.get("param")))

        if self.broken:
            return AiohttpClientMockResponse(method, url, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        if self.throttle > 0:
            self.throttle -= 1
            return AiohttpClientMockResponse(method, url, status=HTTPStatus.TOO_MANY_REQUESTS)
        if is_login:
            return AiohttpClientMockResponse(method, url, json=self._login(data))
        if not self.session:
            body = {"Code": CODE_NOT_LOGGED_IN, "Message": "not logged in"}
            return AiohttpClientMockResponse(method, url, json=body)
        assert data.get("version") == "1.0"
        handler = getattr(self, f"_{data['method']}")
        return AiohttpClientMockResponse(method, url, json=handler(data["param"]))

    def _login(self, data: Any) -> dict[str, Any]:
        password = _PRIVATE_KEY.decrypt(
            base64.b64decode(data["password"]), padding.PKCS1v15()
        ).decode()
        if data.get("account") != self.username or password != self.password:
            self.session = False
            return {"code": -1, "data": None, "message": "account or password error"}
        self.session = True
        self.logins += 1
        return {"code": 0, "data": {"gid": "gid-4711"}}

    def _GetTrackInfoList(self, param: Any) -> dict[str, Any]:
        assert param["IsArchived"] is False
        rows = list(self.packages.values())
        page = int(param["Page"])
        size = int(param["PerPage"])
        assert size == _PAGE_SIZE
        return {
            "Code": 0,
            "Json": rows[(page - 1) * size : page * size],
            "pageInfo": {"TotalCount": len(rows)},
        }

    def _AddTrackNo(self, param: Any) -> dict[str, Any]:
        (number,) = param["TrackNos"]
        if number in self.invalid or number in self.packages:
            return {"Code": CODE_INVALID_NUMBER}
        self.add(number, state=0)
        return {"Code": 0}

    def _SetTrackArchived(self, param: Any) -> dict[str, Any]:
        ids = set(param["TrackInfoIds"])
        for number, row in list(self.packages.items()):
            if row["FTrackInfoId"] in ids:
                self.archived[number] = self.packages.pop(number)
        return {"Code": 0}

    def expire_session(self) -> None:
        """Die Sitzung laeuft ab; die naechste Konto-Anfrage wird abgelehnt."""
        self.session = False
