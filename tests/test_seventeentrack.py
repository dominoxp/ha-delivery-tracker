"""Tests der 17TRACK-Quelle — ausschliesslich gegen das Fake aus `fake_seventeentrack`."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.delivery_tracker.carriers import async_get_registry
from custom_components.delivery_tracker.const import (
    DOMAIN,
    ISSUE_API_KEY_INVALID,
    ISSUE_API_QUOTA_EXHAUSTED,
    SOURCE_IMAP,
    SOURCE_SEVENTEENTRACK,
    SUBENTRY_API_SOURCE,
)
from custom_components.delivery_tracker.coordinator import SourceState
from custom_components.delivery_tracker.diagnostics import async_get_config_entry_diagnostics
from custom_components.delivery_tracker.issues import source_issue_id
from custom_components.delivery_tracker.merge import merge_shipments
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus, SourceRecord
from custom_components.delivery_tracker.sources import (
    API_SOURCE_FACTORIES,
    SeventeenTrackSource,
    SourceAuthError,
    SourceContext,
    SourceQuotaError,
    SourceRateLimitError,
)
from custom_components.delivery_tracker.sources.seventeentrack_source import (
    create_source,
    load_mapping,
)
from tests.fake_imap import FakeImapServer
from tests.fake_seventeentrack import VALID_API_KEY, FakeSeventeenTrack
from tests.test_coordinator import (
    handle_by_kind,
    mailbox,
    make_entry,
    setup_entry,
    wait_for,
)

DHL_NUMBER = "00340434161094042557"
UPS_NUMBER = "1Z999AA10123456784"
MAIL_TIME = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

API_SOURCE = ConfigSubentryData(
    data={"source_type": SOURCE_SEVENTEENTRACK, "api_key": VALID_API_KEY, "scan_interval": 0},
    subentry_type=SUBENTRY_API_SOURCE,
    title="17TRACK",
    unique_id=SOURCE_SEVENTEENTRACK,
)


# ----------------------------------------------------------------------
# Fixtures und Hilfen
# ----------------------------------------------------------------------


@pytest.fixture
def api(aioclient_mock: AiohttpClientMocker) -> FakeSeventeenTrack:
    """Das Fake der API, eingehaengt in die Session von HA."""
    fake = FakeSeventeenTrack()
    fake.install(aioclient_mock)
    return fake


def mail_shipment(
    number: str = DHL_NUMBER,
    *,
    carrier: str = "dhl",
    status: ShipmentStatus = ShipmentStatus.LABEL_CREATED,
) -> Shipment:
    """Eine Sendung, wie sie die Mail-Pipeline liefert — mit Kontext, den nur die Mail kennt."""
    return Shipment(
        id=f"{carrier}_{number.lower()}",
        carrier=carrier,
        tracking_number=number,
        status=status,
        status_text="Sendungsankündigung",
        sender="Kaffeerösterei Nord",
        description="Espresso, 1 kg",
        destination_raw="Musterstraße 1, 12345 Musterstadt",
        tracking_url=f"https://www.dhl.de/de/privatkunden.html?piececode={number}",
        sources=[SourceRecord(source_id=SOURCE_IMAP, fetched_at=MAIL_TIME)],
        first_seen=MAIL_TIME,
        last_updated=MAIL_TIME,
    )


async def make_source(
    hass: HomeAssistant, known: list[Shipment] | None = None, *, api_key: str = VALID_API_KEY
) -> SeventeenTrackSource:
    """Eine Quelle mit echter Registry und einem Bestand, den der Test veraendern kann."""
    store = known if known is not None else []
    context = SourceContext(
        known_shipments=lambda: list(store), registry=await async_get_registry(hass)
    )
    source = create_source(hass, {"api_key": api_key}, context)
    await source.async_setup()
    return source


# ----------------------------------------------------------------------
# Zuordnungsdatei
# ----------------------------------------------------------------------


def test_zuordnung_liegt_als_datei_neben_dem_modul() -> None:
    """Carrier-Codes und Status kommen aus `seventeentrack.yaml`, in beide Richtungen."""
    mapping = load_mapping()

    assert mapping.carrier_code("dhl") == 7041
    assert mapping.carrier_code("unbekannt") is None
    assert mapping.carrier_id(100001) == "dhl"
    assert mapping.carrier_id("100002") == "ups"
    assert mapping.carrier_id(4711) is None

    assert mapping.status("InTransit", "InTransit_Other") is ShipmentStatus.IN_TRANSIT
    assert mapping.status("InTransit", "InTransit_CustomsProcessing") is ShipmentStatus.CUSTOMS
    assert mapping.status("Exception", "Exception_Returning") is ShipmentStatus.RETURNED
    assert mapping.status("Exception", "Exception_Other") is ShipmentStatus.DELAYED
    assert mapping.status("DeliveryFailure", "DeliveryFailure_NoBody") is ShipmentStatus.DELAYED
    assert mapping.status("Delivered", None) is ShipmentStatus.DELIVERED
    # Events tragen oft nur den Unterstatus.
    assert mapping.status(None, "AvailableForPickup_Other") is ShipmentStatus.READY_FOR_PICKUP
    assert mapping.status("Erfunden", "Erfunden_Other") is ShipmentStatus.UNKNOWN


# ----------------------------------------------------------------------
# Abgleich: registrieren, uebernehmen, abmelden
# ----------------------------------------------------------------------


async def test_mail_sendung_wird_registriert_und_beim_naechsten_mal_abgefragt(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """Registriert mit Carrier-Code, abgefragt erst im naechsten Durchlauf; Kontext bleibt."""
    mail = mail_shipment()
    source = await make_source(hass, [mail])

    assert await source.async_fetch() == []
    assert api.payloads("register") == [[{"number": DHL_NUMBER, "carrier": 7041}]]
    assert "gettrackinfo" not in api.paths()

    api.set_info(DHL_NUMBER, "dhl_in_transit")
    (fragment,) = await source.async_fetch()

    assert fragment.id == mail.id
    assert fragment.carrier == "dhl"
    assert fragment.status is ShipmentStatus.IN_TRANSIT
    assert fragment.status_text == "Die Sendung wurde im Start-Paketzentrum bearbeitet."
    assert fragment.eta_start == datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    assert fragment.eta_end == datetime(2026, 7, 30, 16, 0, tzinfo=UTC)
    assert [(event.status, event.location) for event in fragment.events] == [
        (ShipmentStatus.IN_TRANSIT, "Bremen, DE"),
        (ShipmentStatus.IN_TRANSIT, "Hamburg"),
    ]
    assert fragment.last_updated == datetime(2026, 7, 29, 4, 12, tzinfo=UTC)
    assert fragment.source_ids == [SOURCE_SEVENTEENTRACK]
    # Was nur die Mail weiss, laesst die API leer.
    assert fragment.sender is fragment.description is fragment.destination_raw is None
    assert fragment.tracking_url is None
    assert len(api.payloads("register")) == 1

    merged = merge_shipments(mail, fragment)
    assert merged.status is ShipmentStatus.IN_TRANSIT
    assert merged.sender == "Kaffeerösterei Nord"
    assert merged.description == "Espresso, 1 kg"
    assert merged.destination_raw == "Musterstraße 1, 12345 Musterstadt"
    assert merged.tracking_url == mail.tracking_url
    assert merged.source_ids == [SOURCE_IMAP, SOURCE_SEVENTEENTRACK]


async def test_sendung_nur_bei_17track_wird_uebernommen(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """Eine Nummer, zu der nie eine Mail kam, bekommt Carrier und ID aus der Zuordnung."""
    api.track(UPS_NUMBER, 100002, "ups_out_for_delivery")
    source = await make_source(hass)

    (shipment,) = await source.async_fetch()

    assert shipment.id == "ups_1z999aa10123456784"
    assert shipment.carrier == "ups"
    assert shipment.status is ShipmentStatus.OUT_FOR_DELIVERY
    assert shipment.events[0].status is ShipmentStatus.CUSTOMS
    assert shipment.tracking_url is not None and UPS_NUMBER in shipment.tracking_url
    assert "register" not in api.paths()


async def test_zugestellte_sendung_wird_abgemeldet(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """Zugestellt laut API: abmelden und danach nicht mehr abfragen."""
    api.track(DHL_NUMBER, 7041, "dhl_delivered")
    source = await make_source(hass)

    (shipment,) = await source.async_fetch()
    assert shipment.status is ShipmentStatus.DELIVERED
    assert shipment.delivered_at == datetime(2026, 7, 30, 9, 5, tzinfo=UTC)
    assert DHL_NUMBER in api.stopped

    api.calls.clear()
    assert await source.async_fetch() == []
    assert "gettrackinfo" not in api.paths()
    assert "register" not in api.paths()


async def test_lokal_abgeschlossene_sendung_wird_ohne_abfrage_abgemeldet(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """Meldet die Mail die Zustellung zuerst, wird die Nummer nicht mehr abgefragt."""
    api.track(DHL_NUMBER, 7041, "dhl_in_transit")
    source = await make_source(hass, [mail_shipment(status=ShipmentStatus.DELIVERED)])

    assert await source.async_fetch() == []
    assert "gettrackinfo" not in api.paths()
    assert DHL_NUMBER in api.stopped


async def test_nummer_ohne_daten_liefert_nichts(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """„NotFound" ohne Ereignis haelt eine Sendung nicht kuenstlich frisch."""
    api.track(DHL_NUMBER, 7041)
    source = await make_source(hass)

    assert await source.async_fetch() == []
    assert "gettrackinfo" in api.paths()


async def test_fehltreffer_kosten_kein_kontingent(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """Falsche Pruefziffer und generische Nummern ohne Pruefziffer werden nicht registriert."""
    known = [
        mail_shipment("00340434161094042558"),
        mail_shipment("AB12345678XY", carrier="generic", status=ShipmentStatus.IN_TRANSIT),
        Shipment(id="amazon_order_1", carrier="amazon", order_number="302-1"),
    ]
    source = await make_source(hass, known)

    assert await source.async_fetch() == []
    assert "register" not in api.paths()
    assert api.quota_remain == api.quota_total


async def test_abgelehnte_nummer_wird_nicht_sofort_wiederholt(
    hass: HomeAssistant, api: FakeSeventeenTrack, freezer: FrozenDateTimeFactory
) -> None:
    """Eine Ablehnung gilt einen Tag lang; danach kommt die Nummer erneut dran."""
    api.undetectable.add(DHL_NUMBER)
    source = await make_source(hass, [mail_shipment()])

    await source.async_fetch()
    await source.async_fetch()
    assert len(api.payloads("register")) == 1

    freezer.tick(timedelta(days=1, seconds=1))
    await source.async_fetch()
    assert len(api.payloads("register")) == 2


async def test_bloecke_zu_hoechstens_40_nummern(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """Viele Nummern werden gebuendelt, aber nie mehr als 40 je Anfrage."""
    for index in range(45):
        api.track(f"JJD{index:018d}", 7041, "dhl_in_transit")
    source = await make_source(hass)

    shipments = await source.async_fetch()

    assert len(shipments) == 45
    assert [len(payload) for payload in api.payloads("gettrackinfo")] == [40, 5]
    assert len(api.payloads("gettracklist")) == 2


# ----------------------------------------------------------------------
# Fehlerbilder
# ----------------------------------------------------------------------


async def test_ungueltiger_schluessel(hass: HomeAssistant, api: FakeSeventeenTrack) -> None:
    """Ein abgelehnter Schluessel ist ein Fehler der Zugangsdaten."""
    with pytest.raises(SourceAuthError):
        await make_source(hass, api_key="F" * 32)


async def test_leeres_kontingent_bremst_nur_das_registrieren(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """Bekannte Nummern laufen weiter, neue warten; mit neuem Kontingent geht es weiter."""
    api.track(UPS_NUMBER, 100002, "ups_out_for_delivery")
    api.quota_remain = 0
    source = await make_source(hass, [mail_shipment()])

    shipments = await source.async_fetch()

    assert [shipment.carrier for shipment in shipments] == ["ups"]
    assert "register" not in api.paths()
    assert isinstance(source.warning, SourceQuotaError)
    assert source.diagnostics()["quota"]["remaining"] == 0

    api.quota_remain = 10
    await source.async_fetch()
    assert DHL_NUMBER in api.tracked
    assert source.warning is None
    assert source.diagnostics()["quota"]["remaining"] == 9


async def test_kontingent_laeuft_beim_registrieren_aus(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """Ablehnungen wegen Kontingent sperren die Nummer nicht, sie wartet nur."""
    api.quota_remain = 1
    source = await make_source(hass, [mail_shipment(), mail_shipment(UPS_NUMBER, carrier="ups")])

    await source.async_fetch()
    assert len(api.tracked) == 1
    assert isinstance(source.warning, SourceQuotaError)
    assert source.diagnostics()["rejected_numbers"] == 0


async def test_drosselung_mit_backoff(
    hass: HomeAssistant, api: FakeSeventeenTrack, freezer: FrozenDateTimeFactory
) -> None:
    """HTTP 429: Pause ohne Anfragen, danach wieder normal — mit wachsender Pause."""
    source = await make_source(hass)
    api.throttle = 1

    with pytest.raises(SourceRateLimitError):
        await source.async_fetch()
    assert source.diagnostics()["backoff_seconds"] == 60

    api.calls.clear()
    with pytest.raises(SourceRateLimitError):
        await source.async_fetch()
    assert api.calls == []

    freezer.tick(timedelta(seconds=61))
    api.throttle = 1
    with pytest.raises(SourceRateLimitError):
        await source.async_fetch()
    assert source.diagnostics()["backoff_seconds"] == 120

    freezer.tick(timedelta(seconds=121))
    assert await source.async_fetch() == []
    assert source.diagnostics()["backoff_seconds"] == 0


# ----------------------------------------------------------------------
# Im Zusammenspiel mit Coordinator, Postfach, Repairs und Diagnostics
# ----------------------------------------------------------------------


@pytest.fixture
async def imap_server(socket_enabled: None) -> Any:
    """Fake-Postfach fuer das Zusammenspiel mit der Mail-Quelle."""
    server = FakeImapServer()
    await server.start()
    yield server
    await server.stop()


async def test_mail_und_api_im_coordinator(
    hass: HomeAssistant,
    api: FakeSeventeenTrack,
    imap_server: FakeImapServer,
    email_fixture: Callable[[str], bytes],
) -> None:
    """Mail gefunden → registriert → API-Status uebernommen, Mailkontext bleibt."""
    imap_server.add_message(email_fixture("dhl/sendungsankuendigung.eml"))
    # Retention aus: Das Zustelldatum steht fest in der Fixture, die Retention
    # rechnet gegen die echte Uhr — sonst hinge das Ergebnis vom Testdatum ab.
    coordinator = await setup_entry(
        hass, make_entry(mailbox(imap_server), API_SOURCE, retention_days=0)
    )
    await wait_for(lambda: bool(coordinator.data.shipments))

    await coordinator.async_refresh()
    assert DHL_NUMBER in api.tracked

    api.set_info(DHL_NUMBER, "dhl_in_transit")
    await coordinator.async_refresh()

    (shipment,) = coordinator.data.shipments
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.sender == "Kaffeerösterei Nord"
    assert shipment.destination_raw == "Musterstraße 1, 12345 Musterstadt"
    assert shipment.source_ids == [SOURCE_IMAP, SOURCE_SEVENTEENTRACK]

    api.set_info(DHL_NUMBER, "dhl_delivered")
    await coordinator.async_refresh()
    assert coordinator.data.shipments[0].status is ShipmentStatus.DELIVERED
    assert DHL_NUMBER in api.stopped

    diagnostics = await async_get_config_entry_diagnostics(hass, coordinator.config_entry)
    (api_handle,) = handle_by_kind(coordinator, SUBENTRY_API_SOURCE)
    details = diagnostics["sources"][api_handle.key]["api"]
    assert details["quota"]["remaining"] == api.quota_total - 1
    assert details["registered"] == 1
    assert DHL_NUMBER not in str(diagnostics)
    assert VALID_API_KEY not in str(diagnostics)


async def test_api_ausfall_laesst_die_mail_quelle_unberuehrt(
    hass: HomeAssistant,
    api: FakeSeventeenTrack,
    imap_server: FakeImapServer,
    email_fixture: Callable[[str], bytes],
) -> None:
    """HTTP 500 bei 17TRACK: Fehler an der API-Quelle, die Mail-Sendung kommt trotzdem."""
    api.broken = True
    imap_server.add_message(email_fixture("dhl/sendungsankuendigung.eml"))
    coordinator = await setup_entry(hass, make_entry(mailbox(imap_server), API_SOURCE))

    await wait_for(lambda: bool(coordinator.data.shipments))
    await coordinator.async_refresh()

    (handle,) = handle_by_kind(coordinator, SUBENTRY_API_SOURCE)
    assert handle.state is SourceState.ERROR
    assert coordinator.data.shipments[0].sender == "Kaffeerösterei Nord"
    assert coordinator.last_update_success is True


def _issues(hass: HomeAssistant) -> list[str]:
    return sorted(
        issue.translation_key or ""
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    )


async def test_drei_fehlerbilder_sind_unterscheidbar(
    hass: HomeAssistant, api: FakeSeventeenTrack, freezer: FrozenDateTimeFactory
) -> None:
    """Drosselung: nur Fehlerzustand. Kontingent: Hinweis, Quelle laeuft. Schluessel: Reauth."""
    coordinator = await setup_entry(hass, make_entry(API_SOURCE))
    (handle,) = handle_by_kind(coordinator, SUBENTRY_API_SOURCE)
    await coordinator.async_refresh()
    assert handle.state is SourceState.OK

    # Drosselung
    api.throttle = 1
    await coordinator.async_refresh()
    assert handle.state is SourceState.ERROR
    assert _issues(hass) == []

    # Kontingent (nach Ablauf des Backoffs) — eine Mail-Sendung wartet auf ihre Registrierung.
    freezer.tick(timedelta(seconds=61))
    coordinator.async_add_shipment(mail_shipment())
    api.quota_remain = 0
    await coordinator.async_refresh()
    assert handle.state is SourceState.OK
    assert _issues(hass) == [ISSUE_API_QUOTA_EXHAUSTED]
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, source_issue_id(handle.key, ISSUE_API_QUOTA_EXHAUSTED)
    )
    assert issue is not None and issue.severity is ir.IssueSeverity.WARNING

    api.quota_remain = 5
    await coordinator.async_refresh()
    assert _issues(hass) == []

    # Schluessel
    api.api_key = "ein anderer Schluessel"
    await coordinator.async_refresh()
    assert handle.state is SourceState.AUTH_FAILED
    assert _issues(hass) == [ISSUE_API_KEY_INVALID]
    await hass.async_block_till_done()
    assert any(
        flow["context"]["source"] == SOURCE_REAUTH
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )


def test_fabrik_ist_eingetragen() -> None:
    """Die Quelle traegt sich beim Import selbst als Fabrik fuer ihren Typ ein."""
    assert API_SOURCE_FACTORIES[SOURCE_SEVENTEENTRACK] is create_source


async def test_ereigniszeit_ohne_utc_feld(hass: HomeAssistant, api: FakeSeventeenTrack) -> None:
    """Fehlt `time_utc`, gilt `time_iso` samt Versatz."""
    info = {
        "latest_status": {"status": "InTransit", "sub_status": "InTransit_Other"},
        "tracking": {
            "providers": [
                {
                    "events": [
                        {"time_iso": "2026-07-29T06:12:00+02:00", "sub_status": "InTransit_Other"}
                    ]
                }
            ]
        },
    }
    api.track(DHL_NUMBER, 7041, info)
    source = await make_source(hass)

    (shipment,) = await source.async_fetch()
    assert shipment.events[0].timestamp == datetime(2026, 7, 29, 4, 12, tzinfo=UTC)
    assert shipment.last_updated == shipment.events[0].timestamp
    assert shipment.first_seen <= dt_util.utcnow()


async def test_abgewaehlter_zusteller_wird_weder_registriert_noch_abgefragt(
    hass: HomeAssistant, api: FakeSeventeenTrack
) -> None:
    """Die API-Quelle laesst abgewaehlte Zusteller ganz in Ruhe."""
    api.track(UPS_NUMBER, 100002, "ups_out_for_delivery")
    context = SourceContext(
        known_shipments=lambda: [mail_shipment()], registry=await async_get_registry(hass)
    )
    source = create_source(
        hass, {"api_key": VALID_API_KEY, "excluded_carriers": ["dhl", "ups"]}, context
    )
    await source.async_setup()

    assert await source.async_fetch() == []
    assert "register" not in api.paths()
    assert "gettrackinfo" not in api.paths()
    assert "stoptrack" not in api.paths()
