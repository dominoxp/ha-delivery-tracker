"""Fake der 17TRACK-API (v2.4) fuer die Tests.

Haengt sich in `aioclient_mock` und spielt die Endpunkte mit eigenem Zustand
nach: registrierte und gestoppte Nummern, Kontingent, Drosselung. Die
Sendungsdaten (`track_info`) kommen aus den Fixtures unter
`fixtures/seventeentrack/` — gegen den echten Dienst laeuft nie ein Test.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any

from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

FIXTURES = Path(__file__).parent / "fixtures" / "seventeentrack"

VALID_API_KEY = "0123456789ABCDEF0123456789ABCDEF"

_URL = re.compile(r"^https://api\.17track\.net/track/v2\.4/")
_PAGE_SIZE = 40

# Carrier, die das Fake bei einer Registrierung ohne Code „erkennt".
_DETECT = ((re.compile(r"^1Z"), 100002), (re.compile(r"^\d{20}$"), 7041))


def load_track_info(name: str) -> dict[str, Any]:
    """Ein `track_info`-Objekt aus den Fixtures."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _rejected(number: str, code: int, message: str) -> dict[str, Any]:
    return {"number": number, "error": {"code": code, "message": message}}


class FakeSeventeenTrack:
    """Zustand und Antworten des Fakes."""

    def __init__(self, *, api_key: str = VALID_API_KEY, quota: int = 100) -> None:
        """Leeres Konto mit vollem Kontingent."""
        self.api_key = api_key
        self.quota_total = quota
        self.quota_remain = quota
        self.tracked: dict[str, int] = {}
        """Aktiv verfolgte Nummern mit ihrem Carrier-Code."""
        self.stopped: dict[str, int] = {}
        self.track_info: dict[str, dict[str, Any]] = {}
        self.undetectable: set[str] = set()
        self.throttle = 0
        """So viele der naechsten Anfragen werden mit HTTP 429 beantwortet."""
        self.broken = False
        """True: Jede Anfrage scheitert mit HTTP 500."""
        self.calls: list[tuple[str, Any]] = []
        self._mocker: AiohttpClientMocker | None = None

    def install(self, mocker: AiohttpClientMocker) -> None:
        """Alle Endpunkte im Mocker anmelden."""
        self._mocker = mocker
        mocker.post(_URL, side_effect=self._handle)

    def track(self, number: str, carrier: int, info: str | dict[str, Any] | None = None) -> None:
        """Eine Nummer liegt schon im Konto (ohne Registrierung durch die Quelle)."""
        self.tracked[number] = carrier
        if info is not None:
            self.set_info(number, info)

    def set_info(self, number: str, info: str | dict[str, Any]) -> None:
        """Den Stand einer Nummer festlegen — Fixture-Name oder fertiges Objekt."""
        self.track_info[number] = load_track_info(info) if isinstance(info, str) else info

    def paths(self) -> list[str]:
        """Aufgerufene Endpunkte in Reihenfolge."""
        return [path for path, _ in self.calls]

    def payloads(self, path: str) -> list[Any]:
        """Alle Anfragen an einen Endpunkt."""
        return [payload for name, payload in self.calls if name == path]

    # ------------------------------------------------------------------

    async def _handle(self, method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        path = url.path.rsplit("/", 1)[-1]
        assert self._mocker is not None
        headers = self._mocker.mock_calls[-1][3] or {}
        self.calls.append((path, data))

        if self.broken:
            return AiohttpClientMockResponse(method, url, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        if self.throttle > 0:
            self.throttle -= 1
            return AiohttpClientMockResponse(
                method, url, status=HTTPStatus.TOO_MANY_REQUESTS, text="Too Many Requests"
            )
        if headers.get("17token") != self.api_key:
            body: dict[str, Any] = {
                "code": -18010002,
                "data": {"errors": [{"code": -18010002, "message": "Invalid security key."}]},
            }
            return AiohttpClientMockResponse(method, url, json=body)

        handler = getattr(self, f"_{path}")
        return AiohttpClientMockResponse(method, url, json=handler(data))

    def _getquota(self, data: Any) -> dict[str, Any]:
        return {
            "code": 0,
            "data": {
                "quota_total": self.quota_total,
                "quota_used": self.quota_total - self.quota_remain,
                "quota_remain": self.quota_remain,
                "today_used": self.quota_total - self.quota_remain,
                "max_track_daily": 10000,
            },
        }

    def _gettracklist(self, data: Any) -> dict[str, Any]:
        assert data.get("tracking_status") == "Tracking"
        numbers = sorted(self.tracked)
        page_no = int(data.get("page_no", 1))
        page = numbers[(page_no - 1) * _PAGE_SIZE : page_no * _PAGE_SIZE]
        return {
            "code": 0,
            "page": {
                "data_total": len(numbers),
                "page_total": max(1, -(-len(numbers) // _PAGE_SIZE)),
                "page_no": page_no,
                "page_size": _PAGE_SIZE,
            },
            "data": {
                "accepted": [
                    {
                        "number": number,
                        "carrier": self.tracked[number],
                        "tracking_status": "Tracking",
                    }
                    for number in page
                ]
            },
        }

    def _register(self, data: Any) -> dict[str, Any]:
        assert len(data) <= _PAGE_SIZE
        accepted, rejected = [], []
        for item in data:
            number = item["number"]
            if number in self.tracked or number in self.stopped:
                rejected.append(_rejected(number, -18019901, "already registered"))
            elif self.quota_remain <= 0:
                rejected.append(_rejected(number, -18019908, "quota is insufficient"))
            elif number in self.undetectable:
                rejected.append(_rejected(number, -18019903, "carrier can not be detected"))
            else:
                carrier = item.get("carrier") or next(
                    (code for pattern, code in _DETECT if pattern.match(number)), 0
                )
                self.tracked[number] = carrier
                self.quota_remain -= 1
                accepted.append({"origin": 1, "number": number, "carrier": carrier})
        return {"code": 0, "data": {"accepted": accepted, "rejected": rejected}}

    def _gettrackinfo(self, data: Any) -> dict[str, Any]:
        assert len(data) <= _PAGE_SIZE
        accepted, rejected = [], []
        for item in data:
            number = item["number"]
            carrier = self.tracked.get(number, self.stopped.get(number))
            if carrier is None:
                rejected.append(_rejected(number, -18019902, "not registered"))
                continue
            info = self.track_info.get(number) or load_track_info("not_found")
            accepted.append({"number": number, "carrier": carrier, "track_info": info})
        return {"code": 0, "data": {"accepted": accepted, "rejected": rejected}}

    def _stoptrack(self, data: Any) -> dict[str, Any]:
        accepted, rejected = [], []
        for item in data:
            number = item["number"]
            if number in self.tracked:
                self.stopped[number] = self.tracked.pop(number)
                accepted.append({"number": number, "carrier": self.stopped[number]})
            else:
                rejected.append(
                    _rejected(number, -18019906, "only tracking numbers can be stopped")
                )
        return {"code": 0, "data": {"accepted": accepted, "rejected": rejected}}
