"""Auslieferung der mitgebrachten Lovelace-Karte.

HACS liefert bei einer Integration keine Frontend-Dateien aus — die Karte
liegt deshalb gebaut im Integrationsordner (`frontend/`, erzeugt von `card/`)
und wird hier unter einer eigenen URL bereitgestellt. Ueber `add_extra_js_url`
laedt das Frontend sie bei jedem Seitenaufruf mit; ein manueller Eintrag unter
den Dashboard-Ressourcen ist nicht noetig.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME: Final = "delivery-tracker-card.js"
CARD_PATH: Final = Path(__file__).parent / "frontend" / CARD_FILENAME
CARD_URL: Final = f"/{DOMAIN}/{CARD_FILENAME}"


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
    add_extra_js_url(hass, f"{CARD_URL}?v={mtime}")
    _LOGGER.debug("Lovelace-Karte unter %s registriert", CARD_URL)


def _card_mtime() -> int | None:
    """Aenderungszeit des Builds als Versionsparameter, `None` ohne Build."""
    try:
        return int(CARD_PATH.stat().st_mtime)
    except FileNotFoundError:
        return None
