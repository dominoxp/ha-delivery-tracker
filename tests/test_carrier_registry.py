"""Tests fuer die Auto-Discovery und die Trefferauswahl der Carrier-Registry."""

from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.delivery_tracker.carriers import (
    CarrierPlugin,
    CarrierRegistry,
    async_get_registry,
    discover_plugins,
)
from custom_components.delivery_tracker.mail import MailMessage
from custom_components.delivery_tracker.models import Shipment

PLUGIN_MODULE = """
from pathlib import Path

from custom_components.delivery_tracker.carriers.base import RulesCarrier

PLUGIN = RulesCarrier.from_directory(Path(__file__).parent)
"""


def _write_plugin(root: Path, name: str, rules: str) -> Path:
    """Ein Plugin-Verzeichnis mit Modul und Regeln anlegen."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(PLUGIN_MODULE, encoding="utf-8")
    (directory / "rules.yaml").write_text(rules, encoding="utf-8")
    return directory


def _mail(**overrides: object) -> MailMessage:
    """Eine Mail mit unverfaenglichen Standardwerten."""
    values: dict[str, object] = {
        "message_id": "<1@example.org>",
        "subject": "Ihre Sendung ist unterwegs",
        "text": "Sendungsnummer AC12345678",
        "from_address": "versand@acme.com",
    }
    values.update(overrides)
    return MailMessage(**values)  # type: ignore[arg-type]


def test_discovery_findet_plugin_im_temporaeren_verzeichnis(tmp_path: Path):
    """Ein Plugin muss allein durch sein Vorhandensein gefunden werden."""
    _write_plugin(tmp_path, "acme", "carrier: acme\nname: ACME Logistik\n")

    plugins = discover_plugins(tmp_path)

    assert [plugin.carrier_id for plugin in plugins] == ["acme"]
    assert plugins[0].name == "ACME Logistik"


def test_discovery_ignoriert_nicht_plugin_verzeichnisse(tmp_path: Path):
    """Ordner ohne `__init__.py` sowie `_`- und `.`-Ordner sind keine Plugins."""
    _write_plugin(tmp_path, "acme", "carrier: acme\nname: ACME\n")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "beispiel.eml").write_text("kein Plugin", encoding="utf-8")
    _write_plugin(tmp_path, "__pycache__", "carrier: nope\nname: Nope\n")

    assert [plugin.carrier_id for plugin in discover_plugins(tmp_path)] == ["acme"]


def test_kaputter_import_bringt_registry_nicht_um(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Ein Plugin mit Importfehler wird geloggt und uebersprungen."""
    kaputt = tmp_path / "kaputt"
    kaputt.mkdir()
    (kaputt / "__init__.py").write_text("import gibt_es_garantiert_nicht\n", encoding="utf-8")
    _write_plugin(tmp_path, "acme", "carrier: acme\nname: ACME\n")

    plugins = discover_plugins(tmp_path)

    assert [plugin.carrier_id for plugin in plugins] == ["acme"]
    assert "kaputt" in caplog.text


def test_kaputte_regeln_bringen_registry_nicht_um(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Auch eine ungueltige `rules.yaml` kostet nur ihr eigenes Plugin."""
    _write_plugin(tmp_path, "schrott", 'carrier: schrott\nname: X\ntracking:\n  patterns: ["("]\n')
    _write_plugin(tmp_path, "acme", "carrier: acme\nname: ACME\n")

    plugins = discover_plugins(tmp_path)

    assert [plugin.carrier_id for plugin in plugins] == ["acme"]
    assert "schrott" in caplog.text


def test_plugin_ohne_konstante_wird_uebersprungen(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Ein Modul ohne `PLUGIN` ist kein Plugin."""
    leer = tmp_path / "leer"
    leer.mkdir()
    (leer / "__init__.py").write_text('"""Nur ein Modul."""\n', encoding="utf-8")

    assert discover_plugins(tmp_path) == []
    assert "PLUGIN" in caplog.text


def test_doppelte_carrier_id_wird_verworfen(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Zwei Plugins mit derselben ID wuerden Sendungs-IDs kollidieren lassen."""
    _write_plugin(tmp_path, "acme_a", "carrier: acme\nname: ACME A\n")
    _write_plugin(tmp_path, "acme_b", "carrier: acme\nname: ACME B\n")

    registry = CarrierRegistry(discover_plugins(tmp_path))

    assert registry.carrier_ids == ["acme"]
    assert "doppelt" in caplog.text


def test_bestes_plugin_gewinnt(tmp_path: Path):
    """Bei mehreren Kandidaten entscheidet die hoehere Bewertung."""
    _write_plugin(
        tmp_path,
        "acme",
        'carrier: acme\nname: ACME\nmatch:\n  from_domains: ["acme.com"]\n'
        '  subject_patterns: ["ihre sendung"]\n',
    )
    _write_plugin(
        tmp_path,
        "generic",
        'carrier: generic\nname: Generisch\nmatch:\n  body_patterns: ["sendungsnummer"]\n',
    )

    registry = CarrierRegistry(discover_plugins(tmp_path))
    matched = registry.match(_mail())

    assert matched is not None
    plugin, score = matched
    assert plugin.carrier_id == "acme"

    generic = registry.get("generic")
    assert generic is not None
    assert score > generic.matches(_mail())


def test_keine_zuordnung_ohne_treffer(tmp_path: Path):
    """Passt kein Plugin, gibt es weder Treffer noch Sendung."""
    _write_plugin(
        tmp_path, "acme", 'carrier: acme\nname: ACME\nmatch:\n  from_domains: ["dhl.de"]\n'
    )

    registry = CarrierRegistry(discover_plugins(tmp_path))

    assert registry.match(_mail()) is None
    assert registry.parse(_mail()) is None


def test_fehler_im_plugin_wird_isoliert(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Wirft ein Plugin, faellt nur dessen Ergebnis aus."""

    class Boom(CarrierPlugin):
        def __init__(self) -> None:
            super().__init__("boom", "Boom")

        def matches(self, mail: MailMessage) -> int:
            raise RuntimeError("kaputt")

        def parse(self, mail: MailMessage) -> Shipment | None:
            raise RuntimeError("kaputt")

    _write_plugin(
        tmp_path,
        "acme",
        'carrier: acme\nname: ACME\nmatch:\n  from_domains: ["acme.com"]\n'
        'tracking:\n  patterns: ["\\\\b(AC\\\\d{8})\\\\b"]\n',
    )
    registry = CarrierRegistry([Boom(), *discover_plugins(tmp_path)])

    shipment = registry.parse(_mail())

    assert shipment is not None
    assert shipment.carrier == "acme"
    assert "boom" in caplog.text


async def test_registry_wird_nur_einmal_ermittelt(hass: HomeAssistant):
    """Mehrfaches Laden der Integration darf die Discovery nicht wiederholen."""
    first = await async_get_registry(hass)
    second = await async_get_registry(hass)

    assert first is second
    # Das mitgelieferte Beispiel-Plugin beweist, dass das eigene Paket gescannt wird.
    assert "example" in first.carrier_ids


async def test_setup_stellt_registry_bereit(hass: HomeAssistant, setup_integration: ConfigEntry):
    """Nach dem Entry-Setup ist die Registry geladen."""
    registry = await async_get_registry(hass)

    assert len(registry) >= 1
    assert registry.get("example") is not None
