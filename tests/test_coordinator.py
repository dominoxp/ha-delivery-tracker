"""Tests des Coordinators und des Entry-Setups mit echten Quellen.

Postfaecher laufen gegen den Fake-IMAP-Server aus `fake_imap`, API-Quellen
sind ein kleines Fake ueber `API_SOURCE_FACTORIES`. Die echte 17TRACK-Quelle
testet `test_seventeentrack`.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delivery_tracker.carriers import CarrierRegistry
from custom_components.delivery_tracker.const import (
    DOMAIN,
    SOURCE_IMAP,
    SOURCE_SEVENTEENTRACK,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_API_SOURCE,
    SUBENTRY_MAILBOX,
)
from custom_components.delivery_tracker.coordinator import (
    DeliveryTrackerCoordinator,
    SourceState,
    build_data,
)
from custom_components.delivery_tracker.entry import GlobalOptions
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus, SourceRecord
from custom_components.delivery_tracker.sources import (
    API_SOURCE_FACTORIES,
    ImapSource,
    ImapState,
    TrackingSource,
)
from tests.fake_imap import FakeImapServer

TIMEOUT = 10.0
DHL_MAIL = "dhl/sendungsankuendigung.eml"


# ----------------------------------------------------------------------
# Fakes und Fixtures
# ----------------------------------------------------------------------


class FakeApiSource(TrackingSource):
    """Eine API-Quelle, deren Ergebnisse der Test vorgibt."""

    def __init__(self) -> None:
        """Leere Quelle."""
        super().__init__(SOURCE_SEVENTEENTRACK)
        self.results: list[Shipment] = []
        self.error: Exception | None = None
        self.setup_calls = 0
        self.fetch_calls = 0
        self.closed = False

    async def async_setup(self) -> None:
        """Zaehlt nur mit."""
        self.setup_calls += 1

    async def async_fetch(self) -> list[Shipment]:
        """Vorgegebene Ergebnisse einmalig ausliefern oder den Fehler werfen."""
        self.fetch_calls += 1
        if self.error is not None:
            raise self.error
        results, self.results = self.results, []
        return results

    async def async_close(self) -> None:
        """Merken, dass geschlossen wurde."""
        self.closed = True


@pytest.fixture(autouse=True)
def enable_sockets(socket_enabled: None) -> None:
    """Die Postfaecher sprechen ueber einen echten Socket auf 127.0.0.1."""


@pytest.fixture
async def imap_server() -> AsyncGenerator[FakeImapServer]:
    """Fake-Server mit zwei Ordnern — zwei Postfach-Subentries auf einem Server."""
    server = FakeImapServer(folders=("INBOX", "Pakete"))
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def api_sources() -> Any:
    """Die Fabrik fuer 17track durch das Fake ersetzen; liefert die erzeugten Quellen."""
    created: list[FakeApiSource] = []

    def _factory(hass: HomeAssistant, data: Mapping[str, Any], context: Any) -> TrackingSource:
        source = FakeApiSource()
        created.append(source)
        return source

    with patch.dict(API_SOURCE_FACTORIES, {SOURCE_SEVENTEENTRACK: _factory}):
        yield created


def mailbox(server: FakeImapServer, folder: str = "INBOX", **overrides: Any) -> ConfigSubentryData:
    """Ein Postfach-Subentry fuer den Fake-Server."""
    data: dict[str, Any] = {
        "server": "127.0.0.1",
        "port": server.port,
        "security": "none",
        "verify_ssl": False,
        "username": server.username,
        "password": server.password,
        "folder": folder,
        "use_idle": True,
        # Lang genug, dass im Test nur Push etwas ausloest.
        "poll_interval": 3600,
        "lookback_days": 3,
    }
    data.update(overrides)
    return ConfigSubentryData(
        data=data,
        subentry_type=SUBENTRY_MAILBOX,
        title=f"{server.username} ({folder})",
        unique_id=f"{server.username}@127.0.0.1/{folder}",
    )


API_SOURCE = ConfigSubentryData(
    # Intervall 0: Jeder Durchlauf fragt die API ab, die Tests steuern das selbst.
    data={"source_type": SOURCE_SEVENTEENTRACK, "api_key": "x" * 32, "scan_interval": 0},
    subentry_type=SUBENTRY_API_SOURCE,
    title="17TRACK",
    unique_id=SOURCE_SEVENTEENTRACK,
)


def make_entry(*subentries: ConfigSubentryData, **options: Any) -> MockConfigEntry:
    """Hub-Entry mit Subentries und Options."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Delivery Tracker",
        data={},
        # `stale_days` aus: Die Fixture-Mails tragen feste Daten aus dem Sommer
        # 2026 und waeren sonst sofort verfallen. Der Retention-Test setzt ihn selbst.
        options={"retention_days": 14, "stale_days": 0, **options},
        subentries_data=list(subentries),
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> DeliveryTrackerCoordinator:
    """Entry laden und den Coordinator zurueckgeben."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry.runtime_data.coordinator


async def wait_for(predicate: Callable[[], bool]) -> None:
    """Warten, bis eine Bedingung erfuellt ist — echte Sockets brauchen echte Zeit.

    Abfragen statt Event: Die Bedingungen haengen an Zustaenden mehrerer Objekte,
    die kein gemeinsames Signal haben.
    """
    step = 0.02
    for _ in range(int(TIMEOUT / step)):
        if predicate():
            return
        await asyncio.sleep(step)
    raise TimeoutError(f"Bedingung nach {TIMEOUT} s nicht erfuellt")


def handle_by_kind(coordinator: DeliveryTrackerCoordinator, kind: str) -> list[Any]:
    """Alle Quellen eines Typs."""
    return [handle for handle in coordinator.sources.values() if handle.kind == kind]


def api_shipment(status: ShipmentStatus, *, at: datetime | None = None) -> Shipment:
    """Ein Fragment, wie es eine API-Quelle liefert."""
    when = at or dt_util.utcnow()
    return Shipment(
        id="dhl_00340434161094042557",
        carrier="dhl",
        tracking_number="00340434161094042557",
        status=status,
        sources=[SourceRecord(source_id=SOURCE_SEVENTEENTRACK, fetched_at=when)],
        first_seen=when,
        last_updated=when,
    )


# ----------------------------------------------------------------------
# Setup, Unload, Fehlerisolierung
# ----------------------------------------------------------------------


async def test_setup_startet_alle_quellen_und_unload_raeumt_ab(
    hass: HomeAssistant, imap_server: FakeImapServer, api_sources: list[FakeApiSource]
) -> None:
    """Zwei Postfaecher und eine API-Quelle laufen an; nach dem Unload laeuft nichts mehr."""
    entry = make_entry(mailbox(imap_server, "INBOX"), mailbox(imap_server, "Pakete"), API_SOURCE)
    coordinator = await setup_entry(hass, entry)

    mailboxes = [handle.source for handle in handle_by_kind(coordinator, SUBENTRY_MAILBOX)]
    assert len(mailboxes) == 2
    assert all(isinstance(source, ImapSource) for source in mailboxes)
    await wait_for(lambda: all(source.state is ImapState.CONNECTED for source in mailboxes))
    await wait_for(lambda: imap_server.idle_starts >= 2)
    await wait_for(lambda: bool(api_sources) and api_sources[0].fetch_calls >= 1)
    assert api_sources[0].setup_calls == 1
    assert entry.runtime_data.sources is coordinator.sources

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert all(source._runner is None for source in mailboxes)
    assert all(source.state is ImapState.DISCONNECTED for source in mailboxes)
    assert api_sources[0].closed is True
    assert not entry._background_tasks
    await wait_for(lambda: imap_server.connections == 0)


async def test_kaputte_quelle_reisst_die_anderen_nicht_mit(
    hass: HomeAssistant,
    imap_server: FakeImapServer,
    api_sources: list[FakeApiSource],
    email_fixture: Callable[[str], bytes],
) -> None:
    """Eine werfende API-Quelle stoppt weder Setup noch die Sendungen des Postfachs."""
    imap_server.add_message(email_fixture(DHL_MAIL))
    real_factory = API_SOURCE_FACTORIES[SOURCE_SEVENTEENTRACK]

    def _broken(hass: HomeAssistant, data: Mapping[str, Any], context: Any) -> TrackingSource:
        source = real_factory(hass, data, context)
        assert isinstance(source, FakeApiSource)
        source.error = RuntimeError("API kaputt")
        return source

    with patch.dict(API_SOURCE_FACTORIES, {SOURCE_SEVENTEENTRACK: _broken}):
        coordinator = await setup_entry(hass, make_entry(mailbox(imap_server), API_SOURCE))

    await wait_for(lambda: bool(coordinator.data.shipments))
    assert coordinator.data.shipments[0].carrier == "dhl"
    assert coordinator.last_update_success is True

    (api,) = handle_by_kind(coordinator, SUBENTRY_API_SOURCE)
    await wait_for(lambda: api.state is SourceState.ERROR)
    assert api.last_error == "API kaputt"
    assert api.failures >= 1
    status = coordinator.data.sources[api.key]
    assert status.state is SourceState.ERROR
    assert status.as_dict()["last_error"] == "API kaputt"


async def test_unbekannter_quellentyp_wird_als_fehler_vermerkt(hass: HomeAssistant) -> None:
    """Ein API-Subentry ohne Implementierung legt das Setup nicht lahm."""
    with patch.dict(API_SOURCE_FACTORIES, clear=True):
        coordinator = await setup_entry(hass, make_entry(API_SOURCE))

    (api,) = handle_by_kind(coordinator, SUBENTRY_API_SOURCE)
    assert api.source is None
    assert api.state is SourceState.FAILED
    assert "seventeentrack" in (api.last_error or "")


# ----------------------------------------------------------------------
# Push, Neustart, Zugangsdaten
# ----------------------------------------------------------------------


async def test_neue_mail_per_idle_aktualisiert_sofort(
    hass: HomeAssistant, imap_server: FakeImapServer, email_fixture: Callable[[str], bytes]
) -> None:
    """Eine Mail, die waehrend IDLE eintrifft, steht ohne Intervall im Ergebnis."""
    coordinator = await setup_entry(hass, make_entry(mailbox(imap_server)))
    (handle,) = handle_by_kind(coordinator, SUBENTRY_MAILBOX)
    await wait_for(lambda: imap_server.idle_starts >= 1)
    assert coordinator.data.shipments == []

    imap_server.add_message(email_fixture(DHL_MAIL))

    # Der Grundtakt liegt bei Minuten — was hier ankommt, kam per Push.
    await wait_for(lambda: bool(coordinator.data.shipments))
    (shipment,) = coordinator.data.shipments
    assert shipment.carrier == "dhl"
    assert [change.shipment_id for change in coordinator.data.status_changes] == [shipment.id]
    assert handle.state is SourceState.OK
    assert imap_server.mailbox().messages[0].seen


async def test_reload_verliert_nichts_und_legt_nichts_doppelt_an(
    hass: HomeAssistant, imap_server: FakeImapServer, email_fixture: Callable[[str], bytes]
) -> None:
    """Nach einem Reload sind die Sendungen unveraendert da, der Fortschritt bleibt."""
    imap_server.add_message(email_fixture(DHL_MAIL))
    entry = make_entry(mailbox(imap_server))
    coordinator = await setup_entry(hass, entry)
    await wait_for(lambda: bool(coordinator.data.shipments))
    before = [shipment.to_dict() for shipment in coordinator.data.shipments]
    (handle,) = handle_by_kind(coordinator, SUBENTRY_MAILBOX)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    reloaded = entry.runtime_data.coordinator
    assert reloaded is not coordinator
    # Sofort nach dem Laden — vor jedem Abruf — kommt der Bestand aus dem Store.
    assert [shipment.to_dict() for shipment in reloaded.data.shipments] == before
    assert entry.runtime_data.store.source_state(handle.key) == {
        "uid_validity": imap_server.mailbox().uid_validity,
        "last_uid": 1,
    }

    await wait_for(lambda: imap_server.idle_starts >= 2)
    await reloaded.async_refresh()
    assert [shipment.to_dict() for shipment in reloaded.data.shipments] == before
    assert imap_server.fetched_uids == [1]


async def test_bestand_aus_dem_store_ueberlebt_neustart(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Gespeicherte Sendungen stehen nach dem Laden unveraendert bereit."""
    shipment = api_shipment(ShipmentStatus.IN_TRANSIT)
    # Ohne Zielorte landet sie beim Laden ohnehin bei „Unbekannt".
    shipment.destination_id = "unknown"
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {"shipments": [shipment.to_dict()]},
    }
    entry = make_entry()
    coordinator = await setup_entry(hass, entry)
    assert [item.to_dict() for item in coordinator.data.shipments] == [shipment.to_dict()]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    data = entry.runtime_data.coordinator.data
    assert [item.to_dict() for item in data.shipments] == [shipment.to_dict()]


async def test_falsche_zugangsdaten_fuehren_zum_reauth(
    hass: HomeAssistant, imap_server: FakeImapServer
) -> None:
    """Abgelehnte Anmeldung: Reauth-Flow statt Wiederholschleife; danach laeuft es wieder."""
    entry = make_entry(mailbox(imap_server, password="falsch"))
    coordinator = await setup_entry(hass, entry)
    (handle,) = handle_by_kind(coordinator, SUBENTRY_MAILBOX)

    await wait_for(lambda: handle.state is SourceState.AUTH_FAILED)
    await hass.async_block_till_done()
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]
    assert len(flows) == 1
    assert coordinator.data.sources[handle.key].state is SourceState.AUTH_FAILED
    # Andere Quellen wuerden weiterlaufen: Der Coordinator selbst bleibt gesund.
    assert coordinator.last_update_success is True

    # Keine Wiederholschleife — weitere Durchlaeufe versuchen keine Anmeldung.
    attempts = imap_server.login_attempts
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert imap_server.login_attempts == attempts == 1

    # Der Reauth-Flow fragt das Passwort ab, prueft es und laedt den Entry neu.
    flow_id = flows[0]["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {"password": "noch falsch"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"password": imap_server.password}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    (subentry,) = entry.get_subentries_of_type(SUBENTRY_MAILBOX)
    assert subentry.data["password"] == imap_server.password

    (handle,) = handle_by_kind(entry.runtime_data.coordinator, SUBENTRY_MAILBOX)
    await wait_for(lambda: isinstance(handle.source, ImapSource) and handle.source.available)


# ----------------------------------------------------------------------
# Statuswechsel und Retention
# ----------------------------------------------------------------------


async def test_statuswechsel_steht_im_ergebnis(
    hass: HomeAssistant, api_sources: list[FakeApiSource]
) -> None:
    """Neu, Wechsel und kein Wechsel werden im Ergebnis unterschieden."""
    coordinator = await setup_entry(hass, make_entry(API_SOURCE))
    (api,) = api_sources
    start = dt_util.utcnow()

    api.results = [api_shipment(ShipmentStatus.IN_TRANSIT, at=start)]
    await coordinator.async_refresh()
    (change,) = coordinator.data.status_changes
    assert (change.previous, change.current) == (None, ShipmentStatus.IN_TRANSIT)

    api.results = [api_shipment(ShipmentStatus.DELIVERED, at=start + timedelta(hours=2))]
    await coordinator.async_refresh()
    (change,) = coordinator.data.status_changes
    assert (change.previous, change.current) == (
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.DELIVERED,
    )
    assert change.shipment.status is ShipmentStatus.DELIVERED

    api.results = [api_shipment(ShipmentStatus.DELIVERED, at=start + timedelta(hours=2))]
    await coordinator.async_refresh()
    assert coordinator.data.status_changes == []


async def test_retention_raeumt_stille_aktive_sendungen_ab(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Aktiv ohne Aktualisierung seit `stale_days` verschwindet, frisch bleibt."""
    now = dt_util.utcnow()
    vergessen = api_shipment(ShipmentStatus.ORDERED, at=now - timedelta(days=31))
    vergessen.id = "amazon_order_vergessen"
    lang = api_shipment(ShipmentStatus.IN_TRANSIT, at=now - timedelta(days=2))
    lang.first_seen = now - timedelta(days=90)
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {"shipments": [vergessen.to_dict(), lang.to_dict()]},
    }
    coordinator = await setup_entry(hass, make_entry(stale_days=30))

    await coordinator.async_refresh()

    # Der erste Durchlauf laeuft schon beim Setup im Hintergrund; ob er oder
    # dieser hier aufgeraeumt hat, ist egal.
    assert [shipment.id for shipment in coordinator.data.shipments] == [lang.id]
    assert coordinator.store.get("amazon_order_vergessen") is None


# ----------------------------------------------------------------------
# Gruppierungen und Optionen
# ----------------------------------------------------------------------


def _shipment(
    shipment_id: str,
    *,
    carrier: str = "dhl",
    status: ShipmentStatus = ShipmentStatus.IN_TRANSIT,
    destination_id: str | None = None,
    eta: datetime | None = None,
    delivered_at: datetime | None = None,
    updated: datetime,
) -> Shipment:
    return Shipment(
        id=shipment_id,
        carrier=carrier,
        status=status,
        destination_id=destination_id,
        eta_start=eta,
        eta_end=eta + timedelta(hours=2) if eta else None,
        delivered_at=delivered_at,
        sources=[SourceRecord(source_id=SOURCE_IMAP, fetched_at=updated)],
        first_seen=updated,
        last_updated=updated,
    )


async def test_gruppierungen_und_sortierung(hass: HomeAssistant) -> None:
    """Gruppen fuer die Entities und die festgelegte Reihenfolge."""
    noon = dt_util.as_utc(dt_util.start_of_local_day() + timedelta(hours=12))
    today: date = dt_util.as_local(noon).date()
    shipments = [
        _shipment("ohne_eta_alt", updated=noon - timedelta(days=2)),
        _shipment("ohne_eta_neu", carrier="ups", updated=noon - timedelta(hours=1)),
        _shipment("morgen", destination_id="buero", eta=noon + timedelta(days=1), updated=noon),
        _shipment("heute", status=ShipmentStatus.OUT_FOR_DELIVERY, eta=noon, updated=noon),
        _shipment("verspaetet", status=ShipmentStatus.DELAYED, updated=noon),
        _shipment(
            "zugestellt",
            status=ShipmentStatus.DELIVERED,
            delivered_at=noon - timedelta(hours=3),
            updated=noon,
        ),
    ]

    data = build_data(shipments, sources={}, now=noon)

    assert [item.id for item in data.active] == [
        "heute",
        "morgen",
        "verspaetet",
        "ohne_eta_neu",
        "ohne_eta_alt",
    ]
    assert [item.id for item in data.shipments][-1] == "ohne_eta_alt"
    assert sorted(data.by_carrier) == ["dhl", "ups"]
    assert [item.id for item in data.by_destination["buero"]] == ["morgen"]
    assert "zugestellt" not in [item.id for item in data.by_destination["unknown"]]
    assert [item.id for item in data.problems] == ["verspaetet"]
    assert [item.id for item in data.with_status(ShipmentStatus.OUT_FOR_DELIVERY)] == ["heute"]
    assert [item.id for item in data.expected_on(today)] == ["heute"]
    assert [item.id for item in data.expected_on(today + timedelta(days=1))] == ["morgen"]
    assert [item.id for item in data.delivered_on(today)] == ["zugestellt"]


def test_abgeschaltete_carrier_fehlen_in_der_registry() -> None:
    """Die Pipeline sieht nur aktive Zusteller; der Fallback hat einen eigenen Schalter."""

    class _Plugin:
        def __init__(self, carrier_id: str) -> None:
            self.carrier_id = carrier_id

    registry = CarrierRegistry([_Plugin(cid) for cid in ("dhl", "dpd", "generic")])  # type: ignore[misc]
    options = GlobalOptions(disabled_carriers=frozenset({"dpd"}), generic_fallback=False)

    assert options.filter_registry(registry).carrier_ids == ["dhl"]
    assert registry.carrier_ids == ["dhl", "dpd", "generic"]
