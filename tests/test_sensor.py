"""Tests der Sensor-Plattform.

Die Sendungen kommen aus dem Store (`hass_storage`), Quellen braucht es dafuer
nicht. Die Uhr steht fest auf einem Tag in Berlin, damit „heute" eindeutig ist.
"""

from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry, ConfigSubentryData
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.delivery_tracker.const import (
    DOMAIN,
    SOURCE_IMAP,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_DESTINATION,
)
from custom_components.delivery_tracker.entry import destination_device_identifier
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus, SourceRecord
from custom_components.delivery_tracker.sensor import COUNTERS

TIME_ZONE = "Europe/Berlin"
# Dienstag, 6. Oktober 2026, 12:00 in Berlin.
NOON = datetime(2026, 10, 6, 10, 0, tzinfo=dt_util.UTC)

HOME = ConfigSubentryData(
    data={"id": "home", "name": "Zuhause", "emails": ["home@example.com"]},
    subentry_type=SUBENTRY_DESTINATION,
    title="Zuhause",
    unique_id="home",
)


def _shipment(
    shipment_id: str,
    carrier: str,
    status: ShipmentStatus,
    *,
    eta: datetime | None = None,
    delivered_at: datetime | None = None,
    updated: datetime = NOON,
    emails: tuple[str, ...] = (),
) -> Shipment:
    return Shipment(
        id=shipment_id,
        carrier=carrier,
        tracking_number=shipment_id.upper(),
        status=status,
        eta_start=eta,
        eta_end=eta + timedelta(hours=2) if eta else None,
        delivered_at=delivered_at,
        recipient_emails=list(emails),
        sources=[SourceRecord(source_id=SOURCE_IMAP, fetched_at=updated)],
        first_seen=updated,
        last_updated=updated,
    )


def beispieldaten() -> list[Shipment]:
    """Je Zaehler mindestens ein Treffer und mindestens ein Fehlgriff."""
    return [
        _shipment(
            "nachmittag",
            "dhl",
            ShipmentStatus.IN_TRANSIT,
            eta=NOON + timedelta(hours=2),
            emails=("home@example.com",),
        ),
        _shipment(
            "vormittag", "dhl", ShipmentStatus.OUT_FOR_DELIVERY, eta=NOON - timedelta(hours=2)
        ),
        _shipment("locker", "amazon", ShipmentStatus.READY_FOR_PICKUP),
        _shipment("morgen", "dpd", ShipmentStatus.DELAYED, eta=NOON + timedelta(days=1)),
        _shipment(
            "zugestellt", "ups", ShipmentStatus.DELIVERED, delivered_at=NOON - timedelta(hours=3)
        ),
        _shipment(
            "gestern", "gls", ShipmentStatus.DELIVERED, delivered_at=NOON - timedelta(days=1)
        ),
        _shipment("retour", "hermes", ShipmentStatus.RETURNED, updated=NOON - timedelta(hours=5)),
        _shipment(
            "gescheitert", "dhl", ShipmentStatus.DELIVERY_FAILED, updated=NOON - timedelta(hours=1)
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


def make_entry(*subentries: ConfigSubentryData, **options: Any) -> MockConfigEntry:
    """Hub-Entry; `stale_days` aus, sonst entscheidet die Retention mit."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Delivery Tracker",
        data={},
        options={"retention_days": 14, "stale_days": 0, **options},
        subentries_data=list(subentries),
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Entry laden und alle Sensoren schreiben lassen."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    """Entity-ID ueber die unique_id — unabhaengig von Namen und Sprache."""
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")


def state_of(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> State:
    """Zustand eines Sensors; scheitert, wenn es ihn nicht gibt."""
    found = entity_id(hass, entry, key)
    assert found is not None, key
    state = hass.states.get(found)
    assert state is not None, key
    return state


def ids(state: State) -> list[str]:
    """IDs aus dem Attribut `shipments`, in dessen Reihenfolge."""
    return [item["id"] for item in state.attributes["shipments"]]


# ----------------------------------------------------------------------
# Zaehler und Attributliste
# ----------------------------------------------------------------------


@pytest.mark.usefixtures("berlin")
async def test_zaehler_mit_testdaten(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Jeder Zaehler zaehlt genau seine Sendungen, in der festgelegten Reihenfolge."""
    store_with(hass_storage, beispieldaten())
    entry = make_entry(HOME)
    await setup_entry(hass, entry)

    expected = {
        # Mit Termin nach eta_start, ohne Termin danach.
        "active_shipments": ["vormittag", "nachmittag", "morgen", "locker"],
        # Auch die abgeschlossenen; ohne Termin bei gleicher Aenderung nach ID.
        "all_shipments": [
            "vormittag",
            "nachmittag",
            "morgen",
            "gestern",
            "locker",
            "zugestellt",
            "gescheitert",
            "retour",
        ],
        "expected_today": ["vormittag", "nachmittag"],
        "out_for_delivery": ["vormittag"],
        "ready_for_pickup": ["locker"],
        "delivered_today": ["zugestellt"],
        # Ohne Termin: juengste Aenderung zuerst.
        "problems": ["morgen", "gescheitert", "retour"],
        "carrier_dhl": ["vormittag", "nachmittag"],
        "carrier_amazon": ["locker"],
        "carrier_ups": [],
        "destination_home": ["nachmittag"],
    }
    for key, shipment_ids in expected.items():
        state = state_of(hass, entry, key)
        assert ids(state) == shipment_ids, key
        assert state.state == str(len(shipment_ids)), key


@pytest.mark.usefixtures("berlin")
async def test_attribut_ist_kompakt(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Das Attribut ist genau `as_summary()` — ohne Events, Quellen und Rohdaten."""
    shipments = beispieldaten()
    store_with(hass_storage, shipments)
    entry = make_entry()
    await setup_entry(hass, entry)

    (item,) = state_of(hass, entry, "ready_for_pickup").attributes["shipments"]
    locker = next(shipment for shipment in shipments if shipment.id == "locker")
    locker.destination_id = "unknown"
    assert item == locker.as_summary()
    for raw in ("events", "sources", "destination_raw", "recipient_emails", "items"):
        assert raw not in item


@pytest.mark.usefixtures("berlin")
async def test_namen_fuer_die_karte(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Jeder Zaehler nennt die aktiven Zusteller und alle Zielorte mit Namen."""
    store_with(hass_storage, beispieldaten())
    entry = make_entry(HOME, disabled_carriers=["dpd"])
    await setup_entry(hass, entry)

    for key in ("active_shipments", "carrier_dhl", "destination_home"):
        attributes = state_of(hass, entry, key).attributes
        assert attributes["destinations"] == {"home": "Zuhause"}, key
        assert attributes["carriers"]["dhl"] == "DHL Paket", key
        assert "dpd" not in attributes["carriers"], key


@pytest.mark.usefixtures("berlin")
async def test_sensoren_haengen_an_den_richtigen_devices(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Zaehler am Haupt-Device, Zielort-Sensor an dessen Device, Diagnose als solche."""
    store_with(hass_storage, beispieldaten())
    entry = make_entry(HOME)
    await setup_entry(hass, entry)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    hub = devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    home = devices.async_get_device(identifiers={destination_device_identifier(entry, "home")})
    assert hub is not None
    assert home is not None

    for description in COUNTERS:
        registered = entities.async_get(entity_id(hass, entry, description.key) or "")
        assert registered is not None
        assert registered.device_id == hub.id
        assert registered.entity_category is None

    registered = entities.async_get(entity_id(hass, entry, "destination_home") or "")
    assert registered is not None
    assert registered.device_id == home.id
    assert registered.config_subentry_id == next(iter(entry.subentries))

    for key in ("last_update", "sources"):
        registered = entities.async_get(entity_id(hass, entry, key) or "")
        assert registered is not None
        assert registered.entity_category is er.EntityCategory.DIAGNOSTIC


@pytest.mark.usefixtures("berlin")
async def test_diagnose_sensoren(hass: HomeAssistant) -> None:
    """Zeitstempel des letzten Durchlaufs; ohne Quellen ist der Zustand unbekannt."""
    entry = make_entry()
    await setup_entry(hass, entry)

    assert dt_util.parse_datetime(state_of(hass, entry, "last_update").state) == NOON
    sources = state_of(hass, entry, "sources")
    assert sources.state == STATE_UNKNOWN
    assert sources.attributes["sources"] == []
    assert sources.attributes["options"] == ["pending", "ok", "error", "auth_failed", "failed"]


# ----------------------------------------------------------------------
# Zielorte und Zusteller kommen und gehen
# ----------------------------------------------------------------------


@pytest.mark.usefixtures("berlin")
async def test_zielort_anlegen_und_loeschen(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Ein neuer Zielort bekommt Device und Sensor, Loeschen raeumt beides ab."""
    store_with(hass_storage, beispieldaten())
    entry = make_entry(HOME)
    await setup_entry(hass, entry)
    assert entity_id(hass, entry, "destination_buero") is None

    buero = ConfigSubentry(
        data={"id": "buero", "name": "Büro", "emails": ["@firma.example"]},
        subentry_type=SUBENTRY_DESTINATION,
        title="Büro",
        unique_id="buero",
    )
    assert hass.config_entries.async_add_subentry(entry, buero)
    await hass.async_block_till_done()

    found = entity_id(hass, entry, "destination_buero")
    assert found is not None
    registered = er.async_get(hass).async_get(found)
    assert registered is not None
    device = dr.async_get(hass).async_get(registered.device_id or "")
    assert device is not None
    assert device.identifiers == {destination_device_identifier(entry, "buero")}
    assert hass.states.get(found) is not None

    assert hass.config_entries.async_remove_subentry(entry, buero.subentry_id)
    await hass.async_block_till_done()

    assert entity_id(hass, entry, "destination_buero") is None
    assert hass.states.get(found) is None
    assert dr.async_get(hass).async_get(device.id) is None
    # Der andere Zielort bleibt unberuehrt.
    assert entity_id(hass, entry, "destination_home") is not None


@pytest.mark.usefixtures("berlin")
async def test_umbenennen_behaelt_die_unique_id(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Ein umbenannter Zielort behaelt Sensor und Entity-ID."""
    store_with(hass_storage, beispieldaten())
    entry = make_entry(HOME)
    await setup_entry(hass, entry)
    before = entity_id(hass, entry, "destination_home")
    assert before is not None

    (subentry,) = entry.get_subentries_of_type(SUBENTRY_DESTINATION)
    assert hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, "name": "Daheim"}, title="Daheim"
    )
    await hass.async_block_till_done()

    assert entity_id(hass, entry, "destination_home") == before
    assert ids(state_of(hass, entry, "destination_home")) == ["nachmittag"]


@pytest.mark.usefixtures("berlin")
async def test_carrier_abwaehlen_und_wieder_einschalten(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Abgewaehlt verschwindet der Sensor, eingeschaltet kommt er unveraendert zurueck."""
    store_with(hass_storage, beispieldaten())
    entry = make_entry()
    await setup_entry(hass, entry)
    before = entity_id(hass, entry, "carrier_dpd")
    assert before is not None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "disabled_carriers": ["dpd"]}
    )
    await hass.async_block_till_done()

    assert entity_id(hass, entry, "carrier_dpd") is None
    assert hass.states.get(before) is None
    assert entity_id(hass, entry, "carrier_dhl") is not None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "disabled_carriers": []}
    )
    await hass.async_block_till_done()

    assert entity_id(hass, entry, "carrier_dpd") == before
    assert ids(state_of(hass, entry, "carrier_dpd")) == ["morgen"]


@pytest.mark.usefixtures("berlin")
async def test_generischer_fallback_hat_eigenen_schalter(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Ohne Fallback gibt es auch keinen Sensor fuer ihn."""
    entry = make_entry(generic_fallback=False)
    await setup_entry(hass, entry)

    assert entity_id(hass, entry, "carrier_generic") is None
    assert entity_id(hass, entry, "carrier_dhl") is not None


# ----------------------------------------------------------------------
# Tageswechsel
# ----------------------------------------------------------------------


async def test_tageswechsel_ohne_neue_mail(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """Um Mitternacht ruecken „heute erwartet" und „heute zugestellt" weiter."""
    await hass.config.async_set_time_zone(TIME_ZONE)
    # Montag, 23:59:50 in Berlin.
    late = datetime(2026, 10, 5, 21, 59, 50, tzinfo=dt_util.UTC)
    freezer.move_to(late)
    store_with(
        hass_storage,
        [
            _shipment("dienstag", "dhl", ShipmentStatus.IN_TRANSIT, eta=NOON, updated=late),
            _shipment(
                "montag",
                "ups",
                ShipmentStatus.DELIVERED,
                delivered_at=late - timedelta(hours=2),
                updated=late,
            ),
        ],
    )
    entry = make_entry()
    await setup_entry(hass, entry)
    assert ids(state_of(hass, entry, "expected_today")) == []
    assert ids(state_of(hass, entry, "delivered_today")) == ["montag"]

    midnight = late + timedelta(seconds=11)
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()

    assert ids(state_of(hass, entry, "expected_today")) == ["dienstag"]
    assert ids(state_of(hass, entry, "delivered_today")) == []
    # Kein neuer Durchlauf — der Wechsel kam allein von der Uhr.
    assert dt_util.parse_datetime(state_of(hass, entry, "last_update").state) < midnight
