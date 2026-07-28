"""Gemeinsame Test-Fixtures."""

from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delivery_tracker.const import DOMAIN

pytest_plugins = ["pytest_homeassistant_custom_component"]

EMAIL_FIXTURES = Path(__file__).parent / "fixtures" / "emails"


@pytest.fixture
def email_fixture() -> Callable[[str], bytes]:
    """Liefert eine anonymisierte Fixture-Mail als Rohbytes."""

    def _load(name: str) -> bytes:
        return (EMAIL_FIXTURES / name).read_bytes()

    return _load


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Custom Integrations in allen Tests laden."""
    yield


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Ein Config-Entry ohne weitere Konfiguration."""
    return MockConfigEntry(domain=DOMAIN, title="Delivery Tracker", data={})


@pytest.fixture
async def setup_integration(hass: HomeAssistant, config_entry: MockConfigEntry) -> ConfigEntry:
    """Den Entry in HA einrichten und zurueckgeben."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
