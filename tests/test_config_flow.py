"""Tests des minimalen Config-Flows."""

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delivery_tracker.const import DOMAIN


async def test_user_flow_legt_entry_an(hass: HomeAssistant) -> None:
    """Der Benutzer-Flow erzeugt einen Entry mit dem eingegebenen Namen."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Pakete"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Pakete"
    assert result["data"] == {}


async def test_nur_eine_instanz(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Ein zweiter Entry wird abgelehnt."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
