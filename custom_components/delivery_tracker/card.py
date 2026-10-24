"""Auslieferung der mitgebrachten Lovelace-Karte.

HACS liefert bei einer Integration keine Frontend-Dateien aus — die Karte
liegt deshalb gebaut im Integrationsordner (`frontend/`, erzeugt von `card/`)
und wird hier unter einer eigenen URL bereitgestellt.

Geladen wird sie als Dashboard-Ressource, die die Integration selbst anlegt
und bei jedem Start auf den aktuellen Build zeigen laesst. Ressourcen laedt
das Frontend erst nach seinem eigenen Start — anders als `add_extra_js_url`,
das parallel zum Frontend laedt: Dort konnte die Karte sich vor dem
Custom-Element-Registry-Polyfill des Frontends registrieren, der danach
`window.customElements` ersetzt, und war in Firefox verschwunden.

Im YAML-Modus der Ressourcen kann die Integration nichts eintragen; dann
bleibt der manuelle Eintrag (siehe README).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME: Final = "delivery-tracker-card.js"
CARD_PATH: Final = Path(__file__).parent / "frontend" / CARD_FILENAME
CARD_URL: Final = f"/{DOMAIN}/{CARD_FILENAME}"
RESOURCE_TYPE: Final = "module"


async def async_register_card(hass: HomeAssistant) -> None:
    """Die Karte bereitstellen, sofern Frontend und Build vorhanden sind.

    Ohne Frontend (etwa in Tests) gibt es niemanden, der sie laden koennte.
    Fehlt der Build — ein Git-Checkout ohne `npm run build` —, laeuft die
    Integration ohne Karte weiter.
    """
    if "frontend" not in hass.config.components or hass.http is None:
        return

    mtime = await hass.async_add_executor_job(_card_mtime)
    if mtime is None:
        _LOGGER.warning(
            "Lovelace-Karte nicht gefunden (%s) — wurde `npm run build` unter card/ ausgefuehrt?",
            CARD_PATH,
        )
        return

    # Ohne Cache-Header: Nach einem Update soll der Browser die neue Datei
    # holen, auch wenn sie zusaetzlich als Ressource ohne Versionsparameter
    # eingetragen ist.
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=False)]
    )
    _LOGGER.debug("Lovelace-Karte unter %s ausgeliefert", CARD_URL)

    resources = await _async_storage_resources(hass)
    if resources is None:
        _LOGGER.info(
            "Lovelace-Ressourcen werden per YAML verwaltet — die Karte muss dort als "
            "Ressource eingetragen werden: url: %s, type: %s",
            CARD_URL,
            RESOURCE_TYPE,
        )
        return
    await _async_ensure_resource(resources, f"{CARD_URL}?v={mtime}")


async def async_unregister_card(hass: HomeAssistant) -> None:
    """Die selbst angelegte Ressource entfernen, wenn die Integration geloescht wird."""
    if (resources := await _async_storage_resources(hass)) is None:
        return
    for item in _card_resources(resources):
        await resources.async_delete_item(item["id"])


async def _async_ensure_resource(resources: ResourceStorageCollection, url: str) -> None:
    """Genau eine Ressource fuer die Karte, die auf den aktuellen Build zeigt.

    Eintraege derselben Datei — auch von Hand angelegte — werden uebernommen
    statt verdoppelt; die Karte soll nur einmal geladen werden.
    """
    existing, *duplicates = _card_resources(resources) or [None]
    for item in duplicates:
        await resources.async_delete_item(item["id"])

    if existing is None:
        await resources.async_create_item({"res_type": RESOURCE_TYPE, "url": url})
        _LOGGER.debug("Lovelace-Ressource %s angelegt", url)
    elif existing["url"] != url or existing["type"] != RESOURCE_TYPE:
        await resources.async_update_item(existing["id"], {"res_type": RESOURCE_TYPE, "url": url})
        _LOGGER.debug("Lovelace-Ressource auf %s aktualisiert", url)


async def _async_storage_resources(hass: HomeAssistant) -> ResourceStorageCollection | None:
    """Die per UI verwalteten Ressourcen, geladen; `None` im YAML-Modus."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None or not isinstance(lovelace.resources, ResourceStorageCollection):
        return None
    # Die Collection laedt erst bei Bedarf; `async_get_info` stoesst das an.
    await lovelace.resources.async_get_info()
    return lovelace.resources


def _card_resources(resources: ResourceStorageCollection) -> list[dict]:
    """Alle Ressourcen, die auf die Karte zeigen — mit oder ohne Versionsparameter."""
    return [item for item in resources.async_items() if item["url"].split("?")[0] == CARD_URL]


def _card_mtime() -> int | None:
    """Aenderungszeit des Builds als Versionsparameter, `None` ohne Build."""
    try:
        return int(CARD_PATH.stat().st_mtime)
    except FileNotFoundError:
        return None
