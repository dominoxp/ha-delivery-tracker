"""Diagnostics-Download: alle Abschnitte, aber kein Geheimnis und keine Adresse."""

import json
from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.delivery_tracker.const import (
    SOURCE_SEVENTEENTRACK,
    SUBENTRY_API_SOURCE,
    SUBENTRY_DESTINATION,
    SUBENTRY_MAILBOX,
)
from custom_components.delivery_tracker.coordinator import DeliveryTrackerCoordinator
from custom_components.delivery_tracker.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.delivery_tracker.entry import (
    destination_device_identifier,
    hub_device_identifier,
)
from custom_components.delivery_tracker.sources import API_SOURCE_FACTORIES, TrackingSource
from tests.fake_imap import FakeImapServer
from tests.test_coordinator import FakeApiSource, mailbox, make_entry, setup_entry, wait_for

USERNAME = "paketpost@example.org"
PASSWORD = "Pa55wort-geheim"
API_KEY = "api-schluessel-0123456789abcdef"
DHL_MAIL = "dhl/sendungsankuendigung.eml"
NEWSLETTER = "newsletter.eml"

HOME = ConfigSubentryData(
    data={
        "id": "zuhause",
        "name": "Zuhause",
        "recipient_names": ["Max Mustermann"],
        "streets": ["Musterstraße 1"],
        "postal_codes": ["12345"],
        "cities": ["Musterstadt"],
        "emails": ["max.mustermann@example.org"],
    },
    subentry_type=SUBENTRY_DESTINATION,
    title="Zuhause",
    unique_id="zuhause",
)

API = ConfigSubentryData(
    data={"source_type": SOURCE_SEVENTEENTRACK, "api_key": API_KEY, "scan_interval": 0},
    subentry_type=SUBENTRY_API_SOURCE,
    title="17TRACK",
    unique_id=SOURCE_SEVENTEENTRACK,
)


@pytest.fixture(autouse=True)
def enable_sockets(socket_enabled: None) -> None:
    """Die Postfaecher sprechen ueber einen echten Socket auf 127.0.0.1."""


@pytest.fixture
async def imap_server() -> AsyncGenerator[FakeImapServer]:
    """Fake-Server, dessen Benutzername eine Mailadresse ist — wie meistens."""
    server = FakeImapServer(username=USERNAME, password=PASSWORD)
    await server.start()
    yield server
    await server.stop()


@pytest.fixture(autouse=True)
def api_source() -> Any:
    """17track durch das Fake ersetzen."""

    def _factory(hass: HomeAssistant, data: Mapping[str, Any], context: Any) -> TrackingSource:
        return FakeApiSource()

    with patch.dict(API_SOURCE_FACTORIES, {SOURCE_SEVENTEENTRACK: _factory}):
        yield


def processed(coordinator: DeliveryTrackerCoordinator) -> int:
    """Wie viele Mails die Postfaecher bisher verarbeitet haben."""
    return sum(
        handle.pipeline.stats.processed
        for handle in coordinator.sources.values()
        if handle.pipeline is not None
    )


@pytest.fixture
async def loaded(
    hass: HomeAssistant, imap_server: FakeImapServer, email_fixture: Callable[[str], bytes]
) -> Any:
    """Entry mit Postfach, Zielort und API-Quelle; die DHL-Mail ist verarbeitet."""
    imap_server.add_message(email_fixture(DHL_MAIL))
    entry = make_entry(mailbox(imap_server), HOME, API)
    coordinator = await setup_entry(hass, entry)
    await wait_for(lambda: bool(coordinator.data.shipments))
    return entry


async def test_alle_abschnitte_sind_enthalten(hass: HomeAssistant, loaded: Any) -> None:
    """Konfiguration, Quellen, Carrier, Sendungszaehler und letzte Aktualisierung."""
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded)
    # Muss ohne eigenen Encoder serialisierbar sein — HA liefert es als JSON aus.
    json.dumps(diagnostics)

    config = diagnostics["config"]
    assert len(config["mailboxes"]) == len(config["api_sources"]) == 1
    assert config["mailboxes"][0]["server"] == "127.0.0.1"
    (destination,) = config["destinations"]
    assert destination["rule_counts"]["streets"] == 1

    assert "dhl" in diagnostics["carriers"]["active"]
    assert set(diagnostics["carriers"]["active"]) <= set(diagnostics["carriers"]["discovered"])

    sources = diagnostics["sources"]
    assert {source["kind"] for source in sources.values()} == {
        SUBENTRY_MAILBOX,
        SUBENTRY_API_SOURCE,
    }
    (mailbox_diag,) = [item for item in sources.values() if item["kind"] == SUBENTRY_MAILBOX]
    assert set(mailbox_diag) >= {"state", "last_error", "imap", "mail_stats", "issue"}

    shipments = diagnostics["shipments"]
    assert shipments["total"] == 1
    assert shipments["by_carrier"] == {"dhl": 1}
    assert sum(shipments["by_status"].values()) == 1
    assert "without_destination" in shipments
    (item,) = shipments["items"]
    assert set(item["tracking_number"]) <= {"9", "A"}

    assert diagnostics["mail_stats"]["matched"] == 1
    assert diagnostics["last_update"]
    assert diagnostics["last_update_success"] is True


async def test_keine_geheimnisse_und_keine_adressdaten(hass: HomeAssistant, loaded: Any) -> None:
    """Gezielt auf die Testwerte: nichts davon steht im Download."""
    shipment = loaded.runtime_data.coordinator.data.shipments[0]
    assert shipment.tracking_number

    dump = json.dumps(await async_get_config_entry_diagnostics(hass, loaded), ensure_ascii=False)

    forbidden = [
        PASSWORD,
        API_KEY,
        USERNAME,
        "paketpost",
        "max.mustermann",
        "Max Mustermann",
        "Musterstraße",
        "Musterstadt",
        "12345",
        shipment.tracking_number,
        shipment.id,
        *shipment.recipient_emails,
    ]
    if shipment.destination_raw:
        forbidden.append(shipment.destination_raw)
    if shipment.pickup_code:
        forbidden.append(shipment.pickup_code)

    assert [value for value in forbidden if value in dump] == []
    assert "@" not in dump


async def test_verworfene_mail_erhoeht_den_zaehler(
    hass: HomeAssistant,
    loaded: Any,
    imap_server: FakeImapServer,
    email_fixture: Callable[[str], bytes],
) -> None:
    """Eine Mail, fuer die kein Zusteller zustaendig ist, steht als verworfen drin."""
    coordinator = loaded.runtime_data.coordinator
    await wait_for(lambda: processed(coordinator) == 1)
    before = (await async_get_config_entry_diagnostics(hass, loaded))["mail_stats"]

    imap_server.add_message(email_fixture(NEWSLETTER))
    await wait_for(lambda: processed(coordinator) == 2)
    after = (await async_get_config_entry_diagnostics(hass, loaded))["mail_stats"]

    assert after["unmatched"] == before["unmatched"] + 1
    assert after["discarded"] == before["discarded"] + 1
    assert after["matched"] == before["matched"]


async def test_device_diagnostics(hass: HomeAssistant, loaded: Any) -> None:
    """Zielort-Device: Regeln gezaehlt und nur seine Sendungen; Hub: der ganze Entry."""
    registry = dr.async_get(hass)
    home = registry.async_get_device({destination_device_identifier(loaded, "zuhause")})
    hub = registry.async_get_device({hub_device_identifier(loaded)})
    assert home is not None
    assert hub is not None

    home_diag = await async_get_device_diagnostics(hass, loaded, home)
    assert home_diag["destination"]["id"] == "zuhause"
    assert home_diag["destination"]["rule_counts"]["emails"] == 1
    expected = sum(
        1
        for shipment in loaded.runtime_data.coordinator.data.shipments
        if shipment.destination_id == "zuhause"
    )
    assert home_diag["shipments"]["total"] == expected
    assert "Musterstraße" not in json.dumps(home_diag, ensure_ascii=False)

    hub_diag = await async_get_device_diagnostics(hass, loaded, hub)
    assert set(hub_diag) >= {"config", "sources", "carriers", "shipments", "mail_stats"}
