"""Tests der Actions und Events (Step 2.5).

Die Sendungen kommen aus dem Store (`hass_storage`) oder aus einer Fake-API-
Quelle; Postfaecher braucht es hier nicht. Die Uhr steht fest auf einem Tag in
Berlin, damit die Reihenfolge der Antworten eindeutig ist.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant, ServiceRegistry
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.delivery_tracker.carriers import async_get_registry
from custom_components.delivery_tracker.const import (
    DOMAIN,
    EVENT_SHIPMENT_ADDED,
    EVENT_SHIPMENT_DELIVERED,
    EVENT_SHIPMENT_UPDATED,
    SOURCE_IMAP,
    SOURCE_MANUAL,
    SOURCE_SEVENTEENTRACK,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_API_SOURCE,
    SUBENTRY_DESTINATION,
)
from custom_components.delivery_tracker.mail import parse_mail
from custom_components.delivery_tracker.models import (
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
    SourceRecord,
)
from custom_components.delivery_tracker.pipeline import MailPipeline
from custom_components.delivery_tracker.services import async_setup_services
from custom_components.delivery_tracker.sources import API_SOURCE_FACTORIES, TrackingSource

TIME_ZONE = "Europe/Berlin"
# Dienstag, 6. Oktober 2026, 12:00 in Berlin.
NOON = datetime(2026, 10, 6, 10, 0, tzinfo=dt_util.UTC)

DHL_MAIL = "dhl/sendungsankuendigung.eml"
# NVE mit stimmender GS1-Pruefziffer.
DHL_NUMBER = "00340434161094042557"

HOME = ConfigSubentryData(
    data={"id": "home", "name": "Zuhause", "emails": ["home@example.com"]},
    subentry_type=SUBENTRY_DESTINATION,
    title="Zuhause",
    unique_id="home",
)
OFFICE = ConfigSubentryData(
    data={"id": "office", "name": "Büro", "emails": ["@firma.example"]},
    subentry_type=SUBENTRY_DESTINATION,
    title="Büro",
    unique_id="office",
)
API_SOURCE = ConfigSubentryData(
    # Langes Intervall: Abgerufen wird nur, wenn der Test es ausdruecklich will.
    data={"source_type": SOURCE_SEVENTEENTRACK, "api_key": "x" * 32, "scan_interval": 3600},
    subentry_type=SUBENTRY_API_SOURCE,
    title="17TRACK",
    unique_id=SOURCE_SEVENTEENTRACK,
)


# ----------------------------------------------------------------------
# Fakes, Fixtures und Helfer
# ----------------------------------------------------------------------


class FakeApiSource(TrackingSource):
    """Eine API-Quelle, deren Ergebnisse der Test vorgibt."""

    def __init__(self) -> None:
        """Leere Quelle."""
        super().__init__(SOURCE_SEVENTEENTRACK)
        self.results: list[Shipment] = []
        self.fetch_calls = 0

    async def async_setup(self) -> None:
        """Nichts zu tun."""

    async def async_fetch(self) -> list[Shipment]:
        """Vorgegebene Ergebnisse einmalig ausliefern."""
        self.fetch_calls += 1
        results, self.results = self.results, []
        return results

    async def async_close(self) -> None:
        """Nichts zu tun."""


@pytest.fixture
def api_sources() -> Any:
    """Die Fabrik fuer 17track durch das Fake ersetzen; liefert die erzeugten Quellen."""
    created: list[FakeApiSource] = []

    def _factory(hass: HomeAssistant, data: Mapping[str, Any]) -> TrackingSource:
        source = FakeApiSource()
        created.append(source)
        return source

    with patch.dict(API_SOURCE_FACTORIES, {SOURCE_SEVENTEENTRACK: _factory}):
        yield created


@pytest.fixture(autouse=True)
async def berlin(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Zeitzone Berlin, Uhr auf Dienstagmittag."""
    await hass.config.async_set_time_zone(TIME_ZONE)
    freezer.move_to(NOON)


def _shipment(
    shipment_id: str,
    carrier: str,
    status: ShipmentStatus,
    *,
    eta: datetime | None = None,
    emails: tuple[str, ...] = (),
    events: tuple[ShipmentEvent, ...] = (),
) -> Shipment:
    return Shipment(
        id=shipment_id,
        carrier=carrier,
        tracking_number=shipment_id.upper(),
        status=status,
        eta_start=eta,
        eta_end=eta + timedelta(hours=2) if eta else None,
        delivered_at=NOON - timedelta(hours=1) if status is ShipmentStatus.DELIVERED else None,
        recipient_emails=list(emails),
        events=list(events),
        sources=[SourceRecord(source_id=SOURCE_IMAP, fetched_at=NOON)],
        first_seen=NOON,
        last_updated=NOON,
    )


def beispieldaten() -> list[Shipment]:
    """Vier aktive Sendungen an zwei Zielorte, dazu zwei abgeschlossene."""
    return [
        _shipment(
            "nachmittag",
            "dhl",
            ShipmentStatus.IN_TRANSIT,
            eta=NOON + timedelta(hours=2),
            emails=("home@example.com",),
            events=(
                ShipmentEvent(timestamp=NOON - timedelta(days=1), status=ShipmentStatus.ORDERED),
                ShipmentEvent(timestamp=NOON, status=ShipmentStatus.IN_TRANSIT, text="unterwegs"),
            ),
        ),
        _shipment(
            "vormittag",
            "dhl",
            ShipmentStatus.OUT_FOR_DELIVERY,
            eta=NOON - timedelta(hours=2),
            emails=("home@example.com",),
        ),
        _shipment("buero", "dpd", ShipmentStatus.IN_TRANSIT, emails=("ich@firma.example",)),
        _shipment("locker", "amazon", ShipmentStatus.READY_FOR_PICKUP),
        _shipment("zugestellt", "ups", ShipmentStatus.DELIVERED, emails=("home@example.com",)),
        _shipment("retour", "hermes", ShipmentStatus.RETURNED),
    ]


def store_with(hass_storage: dict[str, Any], shipments: list[Shipment]) -> None:
    """Sendungen so ablegen, wie sie ein frueherer Lauf gespeichert haette."""
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {"shipments": [shipment.to_dict() for shipment in shipments]},
    }


def make_entry(*subentries: ConfigSubentryData) -> MockConfigEntry:
    """Hub-Entry; `stale_days` aus, sonst entscheidet die Retention mit."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Delivery Tracker",
        data={},
        options={"retention_days": 14, "stale_days": 0},
        subentries_data=list(subentries),
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    """Entry laden."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def call(
    hass: HomeAssistant, service: str, data: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Eine Action aufrufen; mit Antwort, wenn sie eine liefert."""
    response = service in ("get_shipments", "test_parser")
    result = await hass.services.async_call(
        DOMAIN, service, data or {}, blocking=True, return_response=response
    )
    await hass.async_block_till_done()
    return result


async def shipment_ids(hass: HomeAssistant, **data: Any) -> list[str]:
    """IDs aus der Antwort von `get_shipments`, in deren Reihenfolge."""
    response = await call(hass, "get_shipments", data)
    assert response is not None
    return [item["id"] for item in response["shipments"]]


def sensor_state(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """Zustand eines Zaehler-Sensors ueber seine unique_id."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


# ----------------------------------------------------------------------
# get_shipments
# ----------------------------------------------------------------------


async def test_ohne_filter_alle_aktiven(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Ohne Filter alle aktiven Sendungen in der festen Reihenfolge, ohne Historie."""
    store_with(hass_storage, beispieldaten())
    entry = await setup_entry(hass, make_entry(HOME, OFFICE))

    response = await call(hass, "get_shipments")
    assert response is not None
    shipments = response["shipments"]
    # Mit Termin nach eta_start, ohne Termin gleich alt — dann nach ID.
    assert [item["id"] for item in shipments] == ["vormittag", "nachmittag", "buero", "locker"]

    first = shipments[1]
    assert first["destination_id"] == "home"
    assert first["destination_name"] == "Zuhause"
    assert first["sources"] == [SOURCE_IMAP]
    assert first["config_entry_id"] == entry.entry_id
    assert "events" not in first
    assert "version" not in first


async def test_filter_auf_adresse_und_status(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Adresse per ID oder Name, Status mehrfach; abgeschlossene nur auf Nachfrage."""
    store_with(hass_storage, beispieldaten())
    await setup_entry(hass, make_entry(HOME, OFFICE))

    assert await shipment_ids(hass, destination="home") == ["vormittag", "nachmittag"]
    assert await shipment_ids(hass, destination="büro") == ["buero"]
    assert await shipment_ids(hass, destination="unknown") == ["locker"]
    assert await shipment_ids(hass, destination=["home"], status=["in_transit"]) == ["nachmittag"]
    assert await shipment_ids(hass, status=["in_transit"]) == ["nachmittag", "buero"]
    assert await shipment_ids(hass, destination="home", status=["delivered"]) == ["zugestellt"]
    assert await shipment_ids(hass, status=["delivered", "returned"]) == ["retour", "zugestellt"]
    assert await shipment_ids(hass, carrier="DHL") == ["vormittag", "nachmittag"]


async def test_include_events_liefert_die_historie(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """`include_events` haengt die volle Historie an, sonst fehlt sie."""
    store_with(hass_storage, beispieldaten())
    await setup_entry(hass, make_entry(HOME))

    response = await call(
        hass,
        "get_shipments",
        {"destination": "home", "status": "in_transit", "include_events": True},
    )
    assert response is not None
    (item,) = response["shipments"]
    assert item["events"] == [
        {
            "timestamp": (NOON - timedelta(days=1)).isoformat(),
            "status": "ordered",
            "text": None,
            "location": None,
        },
        {
            "timestamp": NOON.isoformat(),
            "status": "in_transit",
            "text": "unterwegs",
            "location": None,
        },
    ]


async def test_unbekannte_adresse_wird_abgelehnt(hass: HomeAssistant) -> None:
    """Ein Tippfehler im Filter ist ein Fehler, keine leere Liste."""
    await setup_entry(hass, make_entry(HOME))

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "get_shipments", {"destination": "zuhaus"})
    assert err.value.translation_key == "unknown_destination"


async def test_ohne_geladenen_entry(hass: HomeAssistant) -> None:
    """Die Actions gibt es auch ohne Entry — sie melden dann verstaendlich den Grund."""
    assert await async_setup_component(hass, DOMAIN, {})

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "get_shipments")
    assert err.value.translation_key == "no_entry"


async def test_template_sensor_mit_get_shipments(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Das README-Beispiel: ein trigger-basierter Template-Sensor ueber die Action."""
    store_with(hass_storage, beispieldaten())
    await setup_entry(hass, make_entry(HOME, OFFICE))

    assert await async_setup_component(
        hass,
        "template",
        {
            "template": [
                {
                    "triggers": [{"trigger": "event", "event_type": "pakete_pruefen"}],
                    "actions": [
                        {
                            "action": "delivery_tracker.get_shipments",
                            "data": {"destination": "home"},
                            "response_variable": "result",
                        }
                    ],
                    "sensor": [
                        {
                            "name": "Pakete Zuhause",
                            "state": "{{ result.shipments | count }}",
                            "attributes": {
                                "naechste": "{{ result.shipments[0].id }}",
                                "zusteller": "{{ result.shipments | map(attribute='carrier') "
                                "| unique | list }}",
                            },
                        }
                    ],
                }
            ]
        },
    )
    await hass.async_block_till_done()

    hass.bus.async_fire("pakete_pruefen")
    await hass.async_block_till_done()

    state = hass.states.get("sensor.pakete_zuhause")
    assert state is not None
    assert state.state == "2"
    assert state.attributes["naechste"] == "vormittag"
    assert state.attributes["zusteller"] == ["dhl"]


# ----------------------------------------------------------------------
# add_shipment
# ----------------------------------------------------------------------


async def test_add_shipment_mit_gueltiger_nummer(hass: HomeAssistant) -> None:
    """Zusteller aus dem Format, Zielort als Festlegung, Event „added"."""
    entry = await setup_entry(hass, make_entry(HOME))
    added = async_capture_events(hass, EVENT_SHIPMENT_ADDED)

    await call(
        hass,
        "add_shipment",
        {
            "tracking_number": "0034 0434 1610 9404 2557",
            "destination": "Zuhause",
            "description": "Kaffeemaschine",
        },
    )

    (item,) = (await call(hass, "get_shipments"))["shipments"]  # type: ignore[index]
    assert item["id"] == f"dhl_{DHL_NUMBER}"
    assert item["carrier"] == "dhl"
    assert item["tracking_number"] == DHL_NUMBER
    assert item["description"] == "Kaffeemaschine"
    assert item["destination_id"] == "home"
    assert item["sources"] == [SOURCE_MANUAL]
    assert DHL_NUMBER in item["tracking_url"]
    assert sensor_state(hass, entry, "active_shipments") == "1"

    (event,) = added
    assert event.data["id"] == f"dhl_{DHL_NUMBER}"
    assert event.data["config_entry_id"] == entry.entry_id

    # Die Festlegung uebersteht die Neuzuordnung des naechsten Durchlaufs.
    await entry.runtime_data.coordinator.async_refresh()
    assert await shipment_ids(hass, destination="home") == [f"dhl_{DHL_NUMBER}"]


@pytest.mark.parametrize(
    ("data", "error"),
    [
        # Pruefziffer falsch — weder DHL noch der Fallback nehmen sie.
        ({"tracking_number": "00340434161094042558"}, "unrecognized_tracking_number"),
        ({"tracking_number": "00340434161094042558", "carrier": "dhl"}, "invalid_tracking_number"),
        ({"tracking_number": DHL_NUMBER, "carrier": "ups"}, "invalid_tracking_number"),
        ({"tracking_number": DHL_NUMBER, "carrier": "post_mars"}, "unknown_carrier"),
        ({"tracking_number": "Rechnung 4711"}, "unrecognized_tracking_number"),
        # 14 Ziffern ohne Pruefverfahren: DPD oder Hermes — raten waere falsch.
        ({"tracking_number": "01234567890123"}, "ambiguous_tracking_number"),
        ({"tracking_number": DHL_NUMBER, "destination": "Mond"}, "unknown_destination"),
    ],
)
async def test_add_shipment_lehnt_ab(hass: HomeAssistant, data: dict[str, Any], error: str) -> None:
    """Ungueltige Eingaben legen nichts an und erklaeren, warum."""
    await setup_entry(hass, make_entry(HOME))

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "add_shipment", data)
    assert err.value.translation_key == error
    assert await shipment_ids(hass, status=[status.value for status in ShipmentStatus]) == []


async def test_add_shipment_mit_angegebenem_zusteller(hass: HomeAssistant) -> None:
    """Eine mehrdeutige Nummer wird mit Zusteller angenommen."""
    await setup_entry(hass, make_entry())

    await call(hass, "add_shipment", {"tracking_number": "01234567890123", "carrier": "hermes"})
    assert await shipment_ids(hass) == ["hermes_01234567890123"]


async def test_spaetere_mail_ergaenzt_dieselbe_sendung(
    hass: HomeAssistant,
    api_sources: list[FakeApiSource],
    email_fixture: Callable[[str], bytes],
) -> None:
    """Die Mail zur von Hand angelegten Nummer landet in derselben Sendung."""
    entry = await setup_entry(hass, make_entry(API_SOURCE))
    (source,) = api_sources
    pipeline = MailPipeline(await async_get_registry(hass))
    (from_mail,) = pipeline.process(email_fixture(DHL_MAIL))
    assert from_mail.tracking_number is not None

    await call(
        hass,
        "add_shipment",
        {"tracking_number": from_mail.tracking_number, "description": "Von Hand"},
    )
    updated = async_capture_events(hass, EVENT_SHIPMENT_UPDATED)
    added = async_capture_events(hass, EVENT_SHIPMENT_ADDED)

    # Die Fake-Quelle reicht die Mail-Sendung durch wie ein Postfach.
    source.results = [from_mail]
    await call(hass, "refresh")

    response = await call(hass, "get_shipments", {"status": [s.value for s in ShipmentStatus]})
    assert response is not None
    (item,) = response["shipments"]
    assert item["id"] == from_mail.id
    assert item["status"] == from_mail.status.value
    assert item["sources"] == [SOURCE_IMAP, SOURCE_MANUAL]
    assert added == []
    (event,) = updated
    assert event.data["old_status"] == "unknown"
    assert event.data["new_status"] == from_mail.status.value
    assert entry.runtime_data.store.get(from_mail.id) is not None


# ----------------------------------------------------------------------
# mark_delivered, remove_shipment
# ----------------------------------------------------------------------


async def test_mark_delivered_wirkt_sofort(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Zaehler aendern sich ohne Abruf; „updated" und „delivered" feuern je einmal."""
    store_with(hass_storage, beispieldaten())
    entry = await setup_entry(hass, make_entry(HOME))
    updated = async_capture_events(hass, EVENT_SHIPMENT_UPDATED)
    delivered = async_capture_events(hass, EVENT_SHIPMENT_DELIVERED)
    assert sensor_state(hass, entry, "active_shipments") == "4"
    assert sensor_state(hass, entry, "out_for_delivery") == "1"

    await call(hass, "mark_delivered", {"shipment_id": "vormittag"})

    assert sensor_state(hass, entry, "active_shipments") == "3"
    assert sensor_state(hass, entry, "out_for_delivery") == "0"
    assert sensor_state(hass, entry, "delivered_today") == "2"
    (event,) = updated
    assert (event.data["old_status"], event.data["new_status"]) == (
        "out_for_delivery",
        "delivered",
    )
    (event,) = delivered
    assert event.data["id"] == "vormittag"
    assert event.data["delivered_at"] == NOON.isoformat()

    # Noch einmal: nichts mehr zu tun, kein weiteres Event.
    await call(hass, "mark_delivered", {"shipment_id": "vormittag"})
    assert len(updated) == 1


async def test_mark_delivered_ueberstimmt_rueckschritt(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Auch eine Retoure laesst sich von Hand auf zugestellt setzen."""
    store_with(hass_storage, beispieldaten())
    await setup_entry(hass, make_entry())

    await call(hass, "mark_delivered", {"shipment_id": "retour"})
    assert await shipment_ids(hass, status="delivered") == ["retour", "zugestellt"]


async def test_remove_shipment_wirkt_sofort(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Geloescht ist sofort weg — in der Antwort und im Zaehler."""
    store_with(hass_storage, beispieldaten())
    entry = await setup_entry(hass, make_entry(HOME))

    await call(hass, "remove_shipment", {"shipment_id": "locker"})

    assert sensor_state(hass, entry, "active_shipments") == "3"
    assert sensor_state(hass, entry, "ready_for_pickup") == "0"
    assert "locker" not in await shipment_ids(hass)
    assert entry.runtime_data.store.get("locker") is None


@pytest.mark.parametrize("service", ["mark_delivered", "remove_shipment"])
async def test_unbekannte_sendungs_id(hass: HomeAssistant, service: str) -> None:
    """Eine falsche ID ist ein verstaendlicher Fehler."""
    await setup_entry(hass, make_entry())

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, service, {"shipment_id": "gibt_es_nicht"})
    assert err.value.translation_key == "unknown_shipment"


# ----------------------------------------------------------------------
# refresh
# ----------------------------------------------------------------------


async def test_refresh_ruft_sofort_ab(
    hass: HomeAssistant, api_sources: list[FakeApiSource]
) -> None:
    """Trotz langem Intervall wird abgerufen — alle Quellen oder die genannte."""
    await setup_entry(hass, make_entry(API_SOURCE))
    (source,) = api_sources
    calls = source.fetch_calls

    await call(hass, "refresh")
    assert source.fetch_calls == calls + 1

    await call(hass, "refresh", {"source": "17track"})
    assert source.fetch_calls == calls + 2

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "refresh", {"source": "Postfach"})
    assert err.value.translation_key == "unknown_source"


# ----------------------------------------------------------------------
# test_parser
# ----------------------------------------------------------------------


async def test_test_parser_mit_dhl_mail(
    hass: HomeAssistant, email_fixture: Callable[[str], bytes]
) -> None:
    """Die DHL-Fixture: DHL gewinnt, die extrahierten Felder stehen in der Antwort."""
    await setup_entry(hass, make_entry())
    mail = parse_mail(email_fixture(DHL_MAIL))

    response = await call(
        hass,
        "test_parser",
        {"subject": mail.subject, "sender": mail.from_address, "body": mail.text},
    )

    assert response is not None
    assert response["winner"] == "dhl"
    plugins = response["plugins"]
    assert plugins[0]["carrier"] == "dhl"
    assert plugins[0]["score"] > 0
    assert {plugin["carrier"] for plugin in plugins} >= {"dhl", "amazon", "generic"}
    (shipment,) = response["shipments"]
    pipeline = MailPipeline(await async_get_registry(hass))
    (expected,) = pipeline.process(email_fixture(DHL_MAIL))
    assert shipment["id"] == expected.id
    assert shipment["tracking_number"] == expected.tracking_number
    assert shipment["status"] == expected.status.value
    # Nichts davon landet im Bestand.
    assert await shipment_ids(hass) == []


async def test_test_parser_ohne_treffer(hass: HomeAssistant) -> None:
    """Eine fremde Mail: kein Gewinner, alle Plugins mit Bewertung 0."""
    await setup_entry(hass, make_entry())

    response = await call(
        hass,
        "test_parser",
        {"subject": "Einladung", "sender": "Oma <oma@example.com>", "body": "Kuchen um drei?"},
    )

    assert response is not None
    assert response["winner"] is None
    assert response["shipments"] == []
    assert all(plugin["score"] == 0 for plugin in response["plugins"])


# ----------------------------------------------------------------------
# Events aus den Abrufen
# ----------------------------------------------------------------------


def api_shipment(status: ShipmentStatus, *, at: datetime) -> Shipment:
    """Ein Fragment, wie es eine API-Quelle liefert."""
    return Shipment(
        id=f"dhl_{DHL_NUMBER}",
        carrier="dhl",
        tracking_number=DHL_NUMBER,
        status=status,
        sources=[SourceRecord(source_id=SOURCE_SEVENTEENTRACK, fetched_at=at)],
        first_seen=at,
        last_updated=at,
    )


async def test_events_nur_bei_echten_aenderungen(
    hass: HomeAssistant, api_sources: list[FakeApiSource]
) -> None:
    """Neu: „added"; Wechsel: genau ein „updated"; gleicher Stand: nichts."""
    entry = await setup_entry(hass, make_entry(API_SOURCE))
    (source,) = api_sources
    added = async_capture_events(hass, EVENT_SHIPMENT_ADDED)
    updated = async_capture_events(hass, EVENT_SHIPMENT_UPDATED)
    delivered = async_capture_events(hass, EVENT_SHIPMENT_DELIVERED)

    source.results = [api_shipment(ShipmentStatus.IN_TRANSIT, at=NOON)]
    await call(hass, "refresh")
    (event,) = added
    assert event.data["status"] == "in_transit"
    assert event.data["config_entry_id"] == entry.entry_id
    assert "events" not in event.data
    assert updated == []

    source.results = [api_shipment(ShipmentStatus.OUT_FOR_DELIVERY, at=NOON + timedelta(hours=1))]
    await call(hass, "refresh")
    (event,) = updated
    assert (event.data["old_status"], event.data["new_status"]) == (
        "in_transit",
        "out_for_delivery",
    )
    assert event.data["status"] == "out_for_delivery"
    assert delivered == []

    # Derselbe Stand noch einmal und ein Abruf ganz ohne Ergebnis: kein Event.
    source.results = [api_shipment(ShipmentStatus.OUT_FOR_DELIVERY, at=NOON + timedelta(hours=1))]
    await call(hass, "refresh")
    await call(hass, "refresh")
    assert (len(added), len(updated), len(delivered)) == (1, 1, 0)

    source.results = [api_shipment(ShipmentStatus.DELIVERED, at=NOON + timedelta(hours=2))]
    await call(hass, "refresh")
    assert len(updated) == 2
    (event,) = delivered
    assert event.data["id"] == f"dhl_{DHL_NUMBER}"


# ----------------------------------------------------------------------
# Registrierung
# ----------------------------------------------------------------------


async def test_actions_einmal_registriert(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Zwei Entries und ein Reload: dieselben Actions, kein doppeltes Registrieren."""
    store_with(hass_storage, beispieldaten())
    # Die Registry hat `__slots__`, gepatcht wird daher die Klasse.
    with patch.object(
        ServiceRegistry,
        "async_register",
        autospec=True,
        side_effect=ServiceRegistry.async_register,
    ) as register:
        first = await setup_entry(hass, make_entry(HOME))
        second = await setup_entry(hass, make_entry())
        assert await hass.config_entries.async_reload(first.entry_id)
        await hass.async_block_till_done()
        async_setup_services(hass)

    # Aufrufe mit `self` vorn: (registry, domain, service, ...).
    names = [args.args[2] for args in register.call_args_list if args.args[1] == DOMAIN]
    assert sorted(names) == sorted(
        [
            "get_shipments",
            "add_shipment",
            "remove_shipment",
            "mark_delivered",
            "refresh",
            "test_parser",
        ]
    )
    assert set(hass.services.async_services_for_domain(DOMAIN)) == set(names)

    # Beide Entries teilen sich das Storage-File; jede Antwort traegt ihre Herkunft.
    response = await call(hass, "get_shipments", {"config_entry_id": second.entry_id})
    assert response is not None
    assert {item["config_entry_id"] for item in response["shipments"]} == {second.entry_id}

    # Mit mehreren Entries muss `add_shipment` wissen, wohin.
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "add_shipment", {"tracking_number": DHL_NUMBER})
    assert err.value.translation_key == "multiple_entries"
    await call(
        hass, "add_shipment", {"tracking_number": DHL_NUMBER, "config_entry_id": first.entry_id}
    )
    assert first.runtime_data.store.get(f"dhl_{DHL_NUMBER}") is not None
