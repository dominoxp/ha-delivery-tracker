"""Tests der Kalender-Plattform.

Wie bei den Sensoren kommen die Sendungen aus dem Store; die Uhr steht auf
Dienstag, 6. Oktober 2026, 12:00 in Berlin.
"""

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delivery_tracker.calendar import shipment_event
from custom_components.delivery_tracker.const import (
    DOMAIN,
    SOURCE_IMAP,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_DESTINATION,
)
from custom_components.delivery_tracker.dates import parse_eta
from custom_components.delivery_tracker.models import (
    Destination,
    Shipment,
    ShipmentStatus,
    SourceRecord,
)

TIME_ZONE = "Europe/Berlin"
# Dienstag, 6. Oktober 2026, 12:00 in Berlin.
NOON = datetime(2026, 10, 6, 10, 0, tzinfo=dt_util.UTC)

HOME = ConfigSubentryData(
    data={"id": "home", "name": "Zuhause", "emails": ["home@example.com"]},
    subentry_type=SUBENTRY_DESTINATION,
    title="Zuhause",
    unique_id="home",
)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """Ein Zeitpunkt im Oktober 2026 in der HA-Zeitzone."""
    return datetime(2026, 10, day, hour, minute, tzinfo=dt_util.DEFAULT_TIME_ZONE)


def _shipment(
    shipment_id: str,
    carrier: str = "dhl",
    status: ShipmentStatus = ShipmentStatus.IN_TRANSIT,
    **fields: Any,
) -> Shipment:
    return Shipment(
        id=shipment_id,
        carrier=carrier,
        status=status,
        sources=[SourceRecord(source_id=SOURCE_IMAP, fetched_at=NOON)],
        first_seen=NOON,
        last_updated=NOON,
        **fields,
    )


def beispieldaten() -> list[Shipment]:
    """Zeitfenster, ganzer Tag, ohne Termin und eine Zustellung von gestern."""
    return [
        _shipment(
            "fenster",
            tracking_number="00340434161094042557",
            sender="Shop",
            status_text="Die Sendung wird heute zugestellt.",
            eta_start=local(6, 14),
            eta_end=local(6, 18),
            recipient_emails=["home@example.com"],
            destination_raw="Musterstraße 1, 12345 Musterstadt",
        ),
        _shipment(
            "tag",
            "dpd",
            eta_start=dt_util.start_of_local_day(date(2026, 10, 7)),
            eta_end=dt_util.start_of_local_day(date(2026, 10, 8)),
        ),
        _shipment("ohne", "gls"),
        _shipment(
            "zugestellt",
            "ups",
            ShipmentStatus.DELIVERED,
            eta_start=local(5, 10),
            eta_end=local(5, 14),
            delivered_at=local(5, 15, 30),
        ),
    ]


@pytest.fixture
async def berlin(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Zeitzone Berlin, Uhr auf Dienstagmittag."""
    await hass.config.async_set_time_zone(TIME_ZONE)
    freezer.move_to(NOON)


def store_with(hass_storage: dict[str, Any], shipments: list[Shipment]) -> None:
    """Sendungen so ablegen, wie sie ein frueherer Lauf gespeichert haette."""
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {"shipments": [shipment.to_dict() for shipment in shipments]},
    }


async def setup_entry(hass: HomeAssistant, *subentries: ConfigSubentryData) -> MockConfigEntry:
    """Hub-Entry laden; `stale_days` aus, sonst entscheidet die Retention mit."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Delivery Tracker",
        data={},
        options={"retention_days": 14, "stale_days": 0},
        subentries_data=list(subentries),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def calendar_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Entity-ID des Kalenders ueber die unique_id."""
    found = er.async_get(hass).async_get_entity_id(
        "calendar", DOMAIN, f"{entry.entry_id}_expected_deliveries"
    )
    assert found is not None
    return found


async def get_events(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Termine ueber den Dienst `calendar.get_events`, wie Automationen ihn nutzen."""
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {"start_date_time": start, "end_date_time": end},
        target={"entity_id": entity_id},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    return response[entity_id]["events"]  # type: ignore[index,return-value]


# ----------------------------------------------------------------------
# Abbildung einer Sendung
# ----------------------------------------------------------------------


@pytest.mark.usefixtures("berlin")
async def test_zeitfenster_wird_termin() -> None:
    """Ein bekanntes Zeitfenster ergibt einen Termin mit genau diesen Zeiten."""
    event = shipment_event(beispieldaten()[0], carrier_name="DHL Paket")

    assert event is not None
    assert not event.all_day
    assert event.start == local(6, 14)
    assert event.end == local(6, 18)
    assert event.uid == "fenster"


@pytest.mark.parametrize("time_zone", ["Europe/Berlin", "Pacific/Auckland", "America/Los_Angeles"])
async def test_nur_datum_wird_ganztaegig(hass: HomeAssistant, time_zone: str) -> None:
    """Ein Datum ohne Uhrzeit ergibt einen ganztaegigen Termin am richtigen Tag."""
    await hass.config.async_set_time_zone(time_zone)
    start, end = parse_eta(
        "Zustellung voraussichtlich am 07.10.2026", now=datetime(2026, 10, 6, tzinfo=dt_util.UTC)
    )

    event = shipment_event(_shipment("tag", eta_start=start, eta_end=end), carrier_name="DPD")

    assert event is not None
    assert event.all_day
    assert event.start == date(2026, 10, 7)
    assert event.end == date(2026, 10, 8)


@pytest.mark.usefixtures("berlin")
async def test_einzelner_zeitpunkt_und_mehrere_tage() -> None:
    """„Gegen 14 Uhr" wird 14 bis 15 Uhr, „Mi bis Do" sind zwei ganze Tage."""
    punkt = shipment_event(
        _shipment("punkt", eta_start=local(6, 14), eta_end=local(6, 14)), carrier_name="DHL"
    )
    assert punkt is not None
    assert (punkt.start, punkt.end) == (local(6, 14), local(6, 15))

    # Ein kurzes Fenster wird ebenfalls auf eine Stunde verlaengert, ein langes bleibt.
    kurz = shipment_event(
        _shipment("kurz", eta_start=local(6, 14), eta_end=local(6, 14, 30)), carrier_name="DHL"
    )
    assert kurz is not None
    assert (kurz.start, kurz.end) == (local(6, 14), local(6, 15))

    tage = shipment_event(
        _shipment(
            "tage",
            eta_start=dt_util.start_of_local_day(date(2026, 10, 7)),
            eta_end=dt_util.start_of_local_day(date(2026, 10, 9)),
        ),
        carrier_name="DHL",
    )
    assert tage is not None
    assert (tage.start, tage.end) == (date(2026, 10, 7), date(2026, 10, 9))


@pytest.mark.usefixtures("berlin")
async def test_ohne_prognose_kein_termin() -> None:
    """Eine Sendung ohne Zustellprognose erscheint nicht im Kalender."""
    assert shipment_event(_shipment("ohne"), carrier_name="GLS") is None


@pytest.mark.usefixtures("berlin")
async def test_titel_beschreibung_und_ort() -> None:
    """Titel kurz und ohne Trackingnummer, Details in der Beschreibung."""
    shipment = beispieldaten()[0]
    shipment.pickup_location = "Packstation 123"
    event = shipment_event(
        shipment, carrier_name="DHL Paket", destination=Destination(id="home", name="Zuhause")
    )

    assert event is not None
    assert event.summary == "DHL Paket: Shop → Zuhause"
    assert "00340434161094042557" not in event.summary
    assert event.description == (
        "Sendungsnummer: 00340434161094042557\n"
        "Status: Die Sendung wird heute zugestellt.\n"
        "Abholort: Packstation 123"
    )
    assert event.location == "Musterstraße 1, 12345 Musterstadt"


@pytest.mark.usefixtures("berlin")
async def test_titel_ohne_absender_und_zielort() -> None:
    """Fehlende Teile entfallen; ein Absender gleich dem Zusteller wird nicht doppelt genannt."""
    amazon = shipment_event(
        _shipment("a", "amazon", sender="Amazon", eta_start=local(6, 14)), carrier_name="Amazon"
    )
    assert amazon is not None
    assert amazon.summary == "Amazon"
    assert amazon.description == "Status: in_transit"
    assert amazon.location is None

    home = Destination(id="home", name="Zuhause")
    ohne_adresse = shipment_event(
        _shipment("b", eta_start=local(6, 14)), carrier_name="DHL Paket", destination=home
    )
    assert ohne_adresse is not None
    assert ohne_adresse.summary == "DHL Paket → Zuhause"
    assert ohne_adresse.location == "Zuhause"


@pytest.mark.usefixtures("berlin")
async def test_zugestellt_wandert_auf_den_zustellzeitpunkt() -> None:
    """Die Zustellung ersetzt die Prognose; ohne Uhrzeit wird es ein ganzer Tag."""
    mit_zeit = shipment_event(beispieldaten()[3], carrier_name="UPS")
    assert mit_zeit is not None
    assert (mit_zeit.start, mit_zeit.end) == (local(5, 15, 30), local(5, 16, 30))

    ohne_zeit = shipment_event(
        _shipment(
            "z",
            status=ShipmentStatus.DELIVERED,
            eta_start=local(5, 10),
            delivered_at=dt_util.start_of_local_day(date(2026, 10, 5)),
        ),
        carrier_name="UPS",
    )
    assert ohne_zeit is not None
    assert (ohne_zeit.start, ohne_zeit.end) == (date(2026, 10, 5), date(2026, 10, 6))


# ----------------------------------------------------------------------
# Entity
# ----------------------------------------------------------------------


@pytest.mark.usefixtures("berlin")
async def test_entity_meldet_naechsten_termin(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Zustand und Attribute zeigen die naechste anstehende Lieferung."""
    store_with(hass_storage, beispieldaten())
    entry = await setup_entry(hass, HOME)

    state = hass.states.get(calendar_id(hass, entry))
    assert state is not None
    assert state.state == STATE_OFF
    assert state.attributes["message"] == "DHL Paket: Shop → Zuhause"
    assert state.attributes["start_time"] == "2026-10-06 14:00:00"
    assert state.attributes["end_time"] == "2026-10-06 18:00:00"
    assert state.attributes["all_day"] is False
    assert state.attributes["location"] == "Musterstraße 1, 12345 Musterstadt"


@pytest.mark.usefixtures("berlin")
async def test_entity_ohne_termine(hass: HomeAssistant) -> None:
    """Ohne Sendungen ist der Kalender aus und hat keine Termin-Attribute."""
    entry = await setup_entry(hass)

    state = hass.states.get(calendar_id(hass, entry))
    assert state is not None
    assert state.state == STATE_OFF
    assert "message" not in state.attributes


@pytest.mark.usefixtures("berlin")
async def test_get_events_ueber_zeitraum(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Alle Termine im Zeitraum, auch die bereits zugestellten."""
    store_with(hass_storage, beispieldaten())
    entry = await setup_entry(hass, HOME)
    entity_id = calendar_id(hass, entry)

    events = await get_events(hass, entity_id, local(5, 0), local(8, 0))
    assert [(event["summary"], event["start"], event["end"]) for event in events] == [
        ("UPS", "2026-10-05T15:30:00+02:00", "2026-10-05T16:30:00+02:00"),
        ("DHL Paket: Shop → Zuhause", "2026-10-06T14:00:00+02:00", "2026-10-06T18:00:00+02:00"),
        ("DPD", "2026-10-07", "2026-10-08"),
    ]

    # Nur der Dienstag: weder der Montag davor noch der ganztaegige Mittwoch.
    events = await get_events(hass, entity_id, local(6, 0), local(7, 0))
    assert [event["summary"] for event in events] == ["DHL Paket: Shop → Zuhause"]

    # Ein Zeitraum, der mitten im Fenster beginnt, enthaelt den Termin.
    events = await get_events(hass, entity_id, local(6, 16), local(6, 17))
    assert [event["summary"] for event in events] == ["DHL Paket: Shop → Zuhause"]


@pytest.mark.usefixtures("berlin")
async def test_neuer_termin_verschiebt_statt_zu_duplizieren(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Ein neuer Zustelltermin aendert den bestehenden Termin."""
    store_with(hass_storage, beispieldaten())
    entry = await setup_entry(hass, HOME)
    entity_id = calendar_id(hass, entry)

    runtime = entry.runtime_data
    shipment = runtime.store.get("fenster")
    assert shipment is not None
    shipment.eta_start = local(8, 9)
    shipment.eta_end = local(8, 12)
    await runtime.coordinator.async_refresh()
    await hass.async_block_till_done()

    events = await get_events(hass, entity_id, local(1, 0), local(20, 0))
    fenster = [event for event in events if event["summary"].startswith("DHL")]
    assert [(event["start"], event["end"]) for event in fenster] == [
        ("2026-10-08T09:00:00+02:00", "2026-10-08T12:00:00+02:00")
    ]
    # Damit ist der ganztaegige Mittwoch jetzt der naechste Termin.
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["message"] == "DPD"
    assert state.attributes["all_day"] is True


async def test_zustellung_endet_mit_der_retention(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """Nach Ablauf der Retention verschwindet auch der vergangene Termin."""
    await hass.config.async_set_time_zone(TIME_ZONE)
    freezer.move_to(NOON)
    store_with(hass_storage, beispieldaten())
    entry = await setup_entry(hass, HOME)
    entity_id = calendar_id(hass, entry)

    events = await get_events(hass, entity_id, local(1, 0), local(20, 0))
    assert "UPS" in [event["summary"] for event in events]

    freezer.move_to(NOON + timedelta(days=15))
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    events = await get_events(hass, entity_id, local(1, 0), local(20, 0))
    assert "UPS" not in [event["summary"] for event in events]
