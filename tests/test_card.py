"""Auslieferung der Lovelace-Karte durch die Integration."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.delivery_tracker import card
from custom_components.delivery_tracker.card import (
    CARD_URL,
    async_register_card,
    async_unregister_card,
)

# Aenderungszeit des vorgetaeuschten Builds; landet als Versionsparameter in der URL.
BUILD_MTIME = 1_791_000_000
VERSIONED_URL = f"{CARD_URL}?v={BUILD_MTIME}"


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


@pytest.fixture
def resources(hass: HomeAssistant) -> ResourceStorageCollection:
    """Die per UI verwalteten Lovelace-Ressourcen, anfangs leer."""
    # Ohne gespeicherte Ressourcen versucht die Collection einen Import aus der
    # Dashboard-Konfiguration; die gibt es hier nicht.
    ll_config = MagicMock(async_load=AsyncMock(side_effect=HomeAssistantError))
    collection = ResourceStorageCollection(hass, ll_config)
    hass.data[LOVELACE_DATA] = SimpleNamespace(resources=collection)
    return collection


def _items(resources: ResourceStorageCollection) -> list[tuple[str, str]]:
    return [(item["type"], item["url"]) for item in resources.async_items()]


async def test_karte_wird_ausgeliefert_und_als_ressource_eingetragen(
    hass: HomeAssistant,
    http: MagicMock,
    built_card: Path,
    resources: ResourceStorageCollection,
) -> None:
    """Mit Frontend und Build: statischer Pfad plus Ressource mit Versionsparameter."""
    await async_register_card(hass)

    (paths,) = http.async_register_static_paths.await_args.args
    assert [(p.url_path, p.path, p.cache_headers) for p in paths] == [
        (CARD_URL, str(built_card), False)
    ]
    assert _items(resources) == [("module", VERSIONED_URL)]


async def test_vorhandene_ressourcen_werden_uebernommen(
    hass: HomeAssistant,
    http: MagicMock,
    built_card: Path,
    resources: ResourceStorageCollection,
) -> None:
    """Alter Build und Handeintrag werden zu genau einer aktuellen Ressource."""
    await resources.async_create_item({"res_type": "module", "url": f"{CARD_URL}?v=1"})
    await resources.async_create_item({"res_type": "js", "url": CARD_URL})
    await resources.async_create_item({"res_type": "module", "url": "/local/andere-karte.js"})

    await async_register_card(hass)

    assert sorted(_items(resources)) == [
        ("module", VERSIONED_URL),
        ("module", "/local/andere-karte.js"),
    ]


async def test_unveraenderte_ressource_bleibt_unangetastet(
    hass: HomeAssistant,
    http: MagicMock,
    built_card: Path,
    resources: ResourceStorageCollection,
) -> None:
    """Ein Neustart ohne neuen Build schreibt nichts."""
    await async_register_card(hass)
    resources.async_update_item = AsyncMock()

    await async_register_card(hass)

    resources.async_update_item.assert_not_awaited()
    assert _items(resources) == [("module", VERSIONED_URL)]


async def test_im_yaml_modus_wird_nur_ausgeliefert(
    hass: HomeAssistant,
    http: MagicMock,
    built_card: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """YAML-Ressourcen sind nicht beschreibbar: ausliefern und auf den Eintrag hinweisen."""
    hass.data[LOVELACE_DATA] = SimpleNamespace(resources=MagicMock(spec=[]))
    caplog.set_level("INFO")

    await async_register_card(hass)

    http.async_register_static_paths.assert_awaited_once()
    assert "per YAML" in caplog.text


async def test_ohne_build_laeuft_die_integration_weiter(
    hass: HomeAssistant,
    http: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resources: ResourceStorageCollection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ein Checkout ohne `npm run build` registriert nichts, warnt aber."""
    monkeypatch.setattr(card, "CARD_PATH", tmp_path / "fehlt.js")
    await async_register_card(hass)

    http.async_register_static_paths.assert_not_awaited()
    assert _items(resources) == []
    assert "npm run build" in caplog.text


async def test_ohne_frontend_wird_nichts_registriert(
    hass: HomeAssistant, built_card: Path, resources: ResourceStorageCollection
) -> None:
    """Ohne geladenes Frontend gibt es niemanden, der die Karte laedt."""
    await async_register_card(hass)

    assert _items(resources) == []


async def test_loeschen_entfernt_nur_die_eigene_ressource(
    hass: HomeAssistant,
    http: MagicMock,
    built_card: Path,
    resources: ResourceStorageCollection,
) -> None:
    """Beim Loeschen der Integration verschwindet ihre Ressource, fremde bleiben."""
    await resources.async_create_item({"res_type": "module", "url": "/local/andere-karte.js"})
    await async_register_card(hass)

    await async_unregister_card(hass)

    assert _items(resources) == [("module", "/local/andere-karte.js")]
