"""Registry der Carrier-Plugins mit Auto-Discovery.

Jedes Unterverzeichnis dieses Pakets, das ein `__init__.py` mit der Konstanten
`PLUGIN` enthaelt, ist ein Zusteller-Plugin. Neue Zusteller kommen also dazu,
ohne dass eine bestehende Datei angefasst werden muss.

Ein einzelnes kaputtes Plugin darf die Integration nie lahmlegen: Import- und
Ladefehler werden geloggt und das Plugin uebersprungen.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final

from homeassistant.core import HomeAssistant

from ..const import DOMAIN
from ..mail import MailMessage
from ..models import Shipment
from .base import CarrierPlugin, RulesCarrier
from .rules import RulesError

__all__ = [
    "PLUGIN_ATTR",
    "CarrierPlugin",
    "CarrierRegistry",
    "Resolution",
    "RulesCarrier",
    "RulesError",
    "async_get_registry",
    "discover_plugins",
]

_LOGGER = logging.getLogger(__name__)

# Name der Modul-Konstanten, unter der ein Plugin seine Instanz bereitstellt.
PLUGIN_ATTR: Final = "PLUGIN"

# Modul-Praefix fuer Plugins ausserhalb dieses Pakets (Tests, spaetere
# Nutzer-Verzeichnisse) — sie haengen an keinem importierbaren Paket.
_EXTERNAL_PREFIX: Final = f"{DOMAIN}_external_carrier_"

_REGISTRY_KEY: Final = f"{DOMAIN}.carrier_registry"
_REGISTRY_LOCK_KEY: Final = f"{DOMAIN}.carrier_registry_lock"

_PACKAGE_PATH: Final = Path(__file__).parent


@dataclass(frozen=True, slots=True)
class Resolution:
    """Ergebnis der Zuordnung einer Mail."""

    shipments: list[Shipment]
    plugin: CarrierPlugin | None = None
    """Das Plugin, das geliefert hat — ohne Sendung das bestbewertete."""
    score: int = 0
    candidates: int = 0
    """Wie viele Plugins sich zustaendig gefuehlt haben; 0 heisst „fremde Mail"."""
    failed: bool = False
    """Mindestens ein Kandidat ist gescheitert, und keiner hat geliefert."""


class CarrierRegistry:
    """Alle geladenen Zusteller-Plugins mit Zugriff und Trefferauswahl."""

    def __init__(self, plugins: list[CarrierPlugin]) -> None:
        """Registry aus einer Plugin-Liste bauen; doppelte IDs werden verworfen."""
        self._plugins: dict[str, CarrierPlugin] = {}
        for plugin in plugins:
            if plugin.carrier_id in self._plugins:
                _LOGGER.error(
                    "Carrier-ID '%s' ist doppelt vergeben, %r wird ignoriert",
                    plugin.carrier_id,
                    plugin,
                )
                continue
            self._plugins[plugin.carrier_id] = plugin

    def __len__(self) -> int:
        """Anzahl geladener Plugins."""
        return len(self._plugins)

    @property
    def plugins(self) -> list[CarrierPlugin]:
        """Alle Plugins, nach Carrier-ID sortiert (stabile Reihenfolge fuer Logs)."""
        return [self._plugins[key] for key in sorted(self._plugins)]

    @property
    def carrier_ids(self) -> list[str]:
        """IDs aller geladenen Zusteller."""
        return sorted(self._plugins)

    def get(self, carrier_id: str) -> CarrierPlugin | None:
        """Ein Plugin ueber seine Carrier-ID holen."""
        return self._plugins.get(carrier_id)

    def candidates(self, mail: MailMessage) -> list[tuple[CarrierPlugin, int]]:
        """Alle Plugins, die sich fuer die Mail zustaendig fuehlen, bestes zuerst.

        Bei gleicher Bewertung entscheidet die Plugin-Prioritaet, danach die
        alphabetisch erste Carrier-ID — die Reihenfolge ist damit reproduzierbar.
        """
        scored: list[tuple[CarrierPlugin, int]] = []
        for plugin in self.plugins:
            try:
                score = plugin.matches(mail)
            except Exception:
                _LOGGER.exception(
                    "Carrier-Plugin '%s' ist bei matches() gescheitert", plugin.carrier_id
                )
                continue
            if score > 0:
                scored.append((plugin, score))
        return sorted(scored, key=lambda item: (-item[1], -item[0].priority, item[0].carrier_id))

    def match(self, mail: MailMessage) -> tuple[CarrierPlugin, int] | None:
        """Das am besten passende Plugin samt Bewertung ermitteln."""
        found = self.candidates(mail)
        return found[0] if found else None

    def resolve(self, mail: MailMessage) -> Resolution:
        """Die Mail auswerten — liefert das beste Plugin nichts, das naechstbeste.

        Die Bewertung sagt nur, wer sich zustaendig *fuehlt*. Ein weiter
        Betreff wie „Ihr Paket" bringt einem Plugin Punkte fuer eine fremde
        Mail, aus der es dann keine Sendung zieht. Wuerde die Registry dort
        aufhoeren, kaeme der generische Fallback mit seiner absichtlich
        niedrigsten Bewertung nie zum Zug. Umgekehrt verdraengt er so nie ein
        spezifisches Plugin: Er wird erst gefragt, wenn alle davor leer bleiben.
        """
        found = self.candidates(mail)
        failed = False
        for plugin, score in found:
            try:
                shipments = plugin.parse_all(mail)
            except Exception:
                _LOGGER.exception(
                    "Carrier-Plugin '%s' ist bei parse() gescheitert", plugin.carrier_id
                )
                failed = True
                continue
            if shipments:
                _LOGGER.debug(
                    "Mail %s -> Plugin '%s' (Bewertung %d), Sendungen: %s",
                    mail.message_id,
                    plugin.carrier_id,
                    score,
                    ", ".join(shipment.id for shipment in shipments),
                )
                return Resolution(shipments, plugin, score, len(found))
            _LOGGER.debug(
                "Mail %s: Plugin '%s' (Bewertung %d) liefert keine Sendung",
                mail.message_id,
                plugin.carrier_id,
                score,
            )

        if not found:
            _LOGGER.debug("Keine Zuordnung fuer Mail %s", mail.message_id)
            return Resolution([])
        plugin, score = found[0]
        return Resolution([], plugin, score, len(found), failed=failed)

    def parse_all(self, mail: MailMessage) -> list[Shipment]:
        """Alle Sendungen der Mail; leer heisst keine Zuordnung oder keine Sendung.

        Der Aufrufer (die Pipeline) unterscheidet beides ueber `resolve`.
        """
        return self.resolve(mail).shipments

    def parse(self, mail: MailMessage) -> Shipment | None:
        """Die erste Sendung der Mail — fuer Aufrufer, die nur eine erwarten."""
        shipments = self.parse_all(mail)
        return shipments[0] if shipments else None


def _import_plugin_module(directory: Path, package: str | None) -> ModuleType:
    """Das `__init__.py` eines Plugin-Verzeichnisses importieren."""
    if package:
        return importlib.import_module(f"{package}.{directory.name}")

    module_name = f"{_EXTERNAL_PREFIX}{directory.name}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        directory / "__init__.py",
        submodule_search_locations=[str(directory)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"{directory}: kein importierbares Modul")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def discover_plugins(directory: Path, *, package: str | None = None) -> list[CarrierPlugin]:
    """Alle Plugins unter `directory` laden (blockierend — im Executor aufrufen).

    `package` ist der Paketname, unter dem die Unterverzeichnisse importierbar
    sind. Ohne Angabe werden sie direkt ueber ihren Dateipfad geladen; das ist
    der Weg fuer Plugin-Verzeichnisse ausserhalb dieses Pakets.
    """
    plugins: list[CarrierPlugin] = []
    if not directory.is_dir():
        _LOGGER.warning("Plugin-Verzeichnis %s existiert nicht", directory)
        return plugins

    for entry in sorted(directory.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if not (entry / "__init__.py").is_file():
            continue

        try:
            module = _import_plugin_module(entry, package)
        except Exception:
            _LOGGER.exception(
                "Carrier-Plugin '%s' konnte nicht geladen werden und wird uebersprungen",
                entry.name,
            )
            continue

        plugin = getattr(module, PLUGIN_ATTR, None)
        if not isinstance(plugin, CarrierPlugin):
            _LOGGER.error(
                "Carrier-Plugin '%s' stellt kein '%s' vom Typ CarrierPlugin bereit",
                entry.name,
                PLUGIN_ATTR,
            )
            continue

        plugins.append(plugin)

    _LOGGER.debug("%d Carrier-Plugins in %s gefunden", len(plugins), directory)
    return plugins


async def async_get_registry(hass: HomeAssistant) -> CarrierRegistry:
    """Die Registry holen — Discovery laeuft genau einmal pro HA-Instanz.

    Das Absuchen des Dateisystems und die YAML-Validierung blockieren, laufen
    daher im Executor.
    """
    if (registry := hass.data.get(_REGISTRY_KEY)) is not None:
        return registry

    lock: asyncio.Lock = hass.data.setdefault(_REGISTRY_LOCK_KEY, asyncio.Lock())
    async with lock:
        # Zweite Pruefung: Waehrend des Wartens kann ein anderer Aufruf fertig sein.
        if (registry := hass.data.get(_REGISTRY_KEY)) is not None:
            return registry

        plugins = await hass.async_add_executor_job(_discover_own_package)
        registry = CarrierRegistry(plugins)
        hass.data[_REGISTRY_KEY] = registry
        _LOGGER.debug("Carrier-Registry geladen: %s", ", ".join(registry.carrier_ids) or "leer")
        return registry


def _discover_own_package() -> list[CarrierPlugin]:
    """Discovery fuer dieses Paket (eigene Funktion, damit sie im Executor laeuft)."""
    return discover_plugins(_PACKAGE_PATH, package=__name__)
