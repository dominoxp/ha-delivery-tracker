"""Recorder-Test der Sensoren — eigenes Modul, weil der Recorder vor `hass` stehen muss.

`conftest.auto_enable_custom_integrations` ist autouse und zieht `hass` sofort
mit. Der Recorder braucht seine Datenbank-URL aber *vor* `hass`; deshalb wird
das Fixture hier mit `recorder_db_url` als erster Abhaengigkeit ueberschrieben.
"""

from collections.abc import Generator
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.delivery_tracker.models import ShipmentStatus
from tests.test_sensor import _shipment, ids, make_entry, setup_entry, state_of, store_with


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_db_url: str, enable_custom_integrations: None
) -> Generator[None]:
    """Wie in `conftest`, nur mit der Recorder-Datenbank vor `hass`."""
    yield


async def test_shipments_landet_nicht_im_recorder(
    recorder_mock: Recorder, hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Der Zaehlerwert wird aufgezeichnet, die Sendungsliste nicht."""
    start = dt_util.utcnow()
    store_with(
        hass_storage,
        [_shipment("unterwegs", "dhl", ShipmentStatus.IN_TRANSIT, updated=start)],
    )
    entry = make_entry()
    await setup_entry(hass, entry)
    active = state_of(hass, entry, "active_shipments")
    assert ids(active) == ["unterwegs"]

    await async_wait_recording_done(hass)
    history = await hass.async_add_executor_job(
        get_significant_states, hass, start, None, [active.entity_id]
    )

    recorded = history[active.entity_id]
    assert [state.state for state in recorded] == ["1"]
    assert all("shipments" not in state.attributes for state in recorded)
