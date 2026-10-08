"""Auslieferung der Lovelace-Karte durch die Integration."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.delivery_tracker import card
from custom_components.delivery_tracker.card import CARD_URL, async_register_card

# Aenderungszeit des vorgetaeuschten Builds; landet als Versionsparameter in der URL.
BUILD_MTIME = 1_791_000_000


@pytest.fixture
def http(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Frontend und HTTP-Server als geladen vortaeuschen."""
    hass.config.components.add("frontend")
    mock = MagicMock()
    mock.async_register_static_paths = AsyncMock()
    monkeypatch.setattr(hass, "http", mock)
    return mock


@pytest.fixture
def built_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Ein Build der Karte an Stelle der echten Datei."""
    path = tmp_path / card.CARD_FILENAME
    path.write_text("// karte", encoding="utf-8")
    os.utime(path, (BUILD_MTIME, BUILD_MTIME))
    monkeypatch.setattr(card, "CARD_PATH", path)
    return path


async def test_karte_wird_ausgeliefert_und_geladen(
    hass: HomeAssistant, http: MagicMock, built_card: Path
) -> None:
    """Mit Frontend und Build: statischer Pfad plus Modul-URL mit Versionsparameter."""
    with patch.object(card, "add_extra_js_url") as add_js:
        await async_register_card(hass)

    (paths,) = http.async_register_static_paths.await_args.args
    assert [(p.url_path, p.path, p.cache_headers) for p in paths] == [
        (CARD_URL, str(built_card), False)
    ]
    add_js.assert_called_once_with(hass, f"{CARD_URL}?v={BUILD_MTIME}")


async def test_ohne_build_laeuft_die_integration_weiter(
    hass: HomeAssistant,
    http: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ein Checkout ohne `npm run build` registriert nichts, warnt aber."""
    monkeypatch.setattr(card, "CARD_PATH", tmp_path / "fehlt.js")
    with patch.object(card, "add_extra_js_url") as add_js:
        await async_register_card(hass)

    http.async_register_static_paths.assert_not_awaited()
    add_js.assert_not_called()
    assert "npm run build" in caplog.text


async def test_ohne_frontend_wird_nichts_registriert(hass: HomeAssistant, built_card: Path) -> None:
    """Ohne geladenes Frontend gibt es niemanden, der die Karte laedt."""
    with patch.object(card, "add_extra_js_url") as add_js:
        await async_register_card(hass)

    add_js.assert_not_called()
