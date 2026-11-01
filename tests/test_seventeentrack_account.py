"""Tests der kostenlosen 17TRACK-Konto-Quelle — nur gegen `fake_seventeentrack_account`."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.delivery_tracker.carriers import async_get_registry
from custom_components.delivery_tracker.const import (
    DOMAIN,
    ISSUE_API_LOGIN_INVALID,
    SOURCE_IMAP,
    SOURCE_SEVENTEENTRACK_ACCOUNT,
    SUBENTRY_API_SOURCE,
)
from custom_components.delivery_tracker.coordinator import SourceState
from custom_components.delivery_tracker.diagnostics import async_get_config_entry_diagnostics
from custom_components.delivery_tracker.merge import merge_shipments
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus
from custom_components.delivery_tracker.sources import (
    API_SOURCE_FACTORIES,
    SeventeenTrackAccountSource,
    SourceAuthError,
    SourceContext,
    SourceError,
    SourceRateLimitError,
)
from custom_components.delivery_tracker.sources import (
    seventeentrack_account_api as account_api,
)
from custom_components.delivery_tracker.sources.seventeentrack_account_api import (
    SeventeenTrackAccountClient,
)
from custom_components.delivery_tracker.sources.seventeentrack_account_source import (
    create_source,
    parse_account_time,
)
from tests.fake_imap import FakeImapServer
from tests.fake_seventeentrack_account import (
    ACCOUNT_PASSWORD,
    ACCOUNT_USERNAME,
    PUBLIC_KEY_PEM,
    FakeSeventeenTrackAccount,
    last_event,
)
from tests.test_config_flow import configure_subentry, start_subentry_flow, subentries_of
from tests.test_coordinator import handle_by_kind, mailbox, make_entry, setup_entry, wait_for
from tests.test_seventeentrack import DHL_NUMBER, UPS_NUMBER, mail_shipment

ACCOUNT_SOURCE = ConfigSubentryData(
    data={
        "source_type": SOURCE_SEVENTEENTRACK_ACCOUNT,
        "username": ACCOUNT_USERNAME,
        "password": ACCOUNT_PASSWORD,
        "scan_interval": 0,
    },
    subentry_type=SUBENTRY_API_SOURCE,
    title="17TRACK-Konto",
    unique_id=SOURCE_SEVENTEENTRACK_ACCOUNT,
)


# ----------------------------------------------------------------------
# Fixtures und Hilfen
# ----------------------------------------------------------------------


@pytest.fixture
def account(
    aioclient_mock: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch
) -> FakeSeventeenTrackAccount:
    """Das Fake des Kontos; Schluessel des Fakes, kein Mindestabstand zwischen Anfragen."""
    monkeypatch.setattr(account_api, "_PUBLIC_KEY_PEM", PUBLIC_KEY_PEM)
    monkeypatch.setattr(account_api, "SEVENTEENTRACK_ACCOUNT_REQUEST_INTERVAL", 0)
    fake = FakeSeventeenTrackAccount()
    fake.install(aioclient_mock)
    return fake


async def make_source(
    hass: HomeAssistant, known: list[Shipment] | None = None, *, password: str = ACCOUNT_PASSWORD
) -> SeventeenTrackAccountSource:
    """Eine angemeldete Quelle mit echter Registry und veraenderbarem Bestand."""
    store = known if known is not None else []
    context = SourceContext(
        known_shipments=lambda: list(store), registry=await async_get_registry(hass)
    )
    source = create_source(hass, {"username": ACCOUNT_USERNAME, "password": password}, context)
    await source.async_setup()
    return source


def local_time(value: str) -> datetime:
    """Eine Zeit ohne Zone, wie das Konto sie liefert, als UTC in der Zone von HA."""
    naive = datetime.strptime(value, "%Y-%m-%d %H:%M")
    return dt_util.as_utc(naive.replace(tzinfo=dt_util.get_default_time_zone()))


# ----------------------------------------------------------------------
# Client und Zeitstempel
# ----------------------------------------------------------------------


async def test_passwort_geht_nur_verschluesselt_hinaus(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount, aioclient_mock: AiohttpClientMocker
) -> None:
    """Die Anmeldung schickt das Passwort RSA-verschluesselt; im Klartext steht es nirgends."""
    client = SeventeenTrackAccountClient(
        async_create_clientsession(hass), ACCOUNT_USERNAME, ACCOUNT_PASSWORD
    )
    await client.async_login()

    assert client.logged_in
    (login,) = [call for call in aioclient_mock.mock_calls if "user-api" in str(call[1])]
    assert login[2]["account"] == ACCOUNT_USERNAME
    assert ACCOUNT_PASSWORD not in str(login)
    assert ACCOUNT_PASSWORD not in repr(client)
    await client.async_close()
    await client.async_close()


def test_zeit_ohne_zone_gilt_als_ortszeit() -> None:
    """Mit und ohne Sekunden; Unlesbares wird `None`."""
    assert parse_account_time("2026-07-29 08:15") == local_time("2026-07-29 08:15")
    assert parse_account_time("2026-07-29 08:15:30") == local_time("2026-07-29 08:15") + (
        timedelta(seconds=30)
    )
    assert parse_account_time("2026-07-29T08:15:00+02:00") == datetime(
        2026, 7, 29, 6, 15, tzinfo=UTC
    )
    assert parse_account_time("gestern") is None
    assert parse_account_time(None) is None


# ----------------------------------------------------------------------
# Abgleich: eintragen, uebernehmen, archivieren
# ----------------------------------------------------------------------


async def test_sendung_nur_im_konto_wird_uebernommen(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Carrier aus der Zuordnung, Status, letztes Ereignis und der Name aus der App."""
    account.add(
        UPS_NUMBER,
        state=10,
        carrier=100002,
        remark="Neue Schuhe",
        event=last_event("2026-07-29 08:15", "Abfahrt vom Standort", "Neuss", "DE"),
    )
    source = await make_source(hass)

    (shipment,) = await source.async_fetch()

    assert shipment.id == "ups_1z999aa10123456784"
    assert shipment.carrier == "ups"
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.status_text == "Abfahrt vom Standort"
    assert shipment.description == "Neue Schuhe"
    (event,) = shipment.events
    assert event.timestamp == local_time("2026-07-29 08:15")
    assert event.location == "Neuss DE"
    assert shipment.last_updated == event.timestamp
    assert shipment.tracking_url is not None and UPS_NUMBER in shipment.tracking_url
    assert shipment.source_ids == [SOURCE_SEVENTEENTRACK_ACCOUNT]
    assert "AddTrackNo" not in account.methods()


async def test_mail_sendung_wird_eingetragen_und_beim_naechsten_mal_uebernommen(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Eingetragen ohne Kontingent; die Mail behaelt Haendler, Produkt und Zieladresse."""
    mail = mail_shipment()
    source = await make_source(hass, [mail])

    assert await source.async_fetch() == []
    assert account.params("AddTrackNo") == [{"TrackNos": [DHL_NUMBER]}]

    account.update(
        DHL_NUMBER,
        FPackageState=10,
        FRemark="Etwas anderes",
        FLastEvent=last_event("2026-07-29 06:12", "Im Paketzentrum bearbeitet", "Bremen"),
    )
    (fragment,) = await source.async_fetch()

    assert fragment.id == mail.id
    assert fragment.status is ShipmentStatus.IN_TRANSIT
    # Der Name aus der App ersetzt keine Beschreibung aus der Mail.
    assert fragment.description is None
    assert fragment.tracking_url is None
    assert len(account.params("AddTrackNo")) == 1

    merged = merge_shipments(mail, fragment)
    assert merged.status is ShipmentStatus.IN_TRANSIT
    assert merged.sender == "Kaffeerösterei Nord"
    assert merged.description == "Espresso, 1 kg"
    assert merged.destination_raw == "Musterstraße 1, 12345 Musterstadt"
    assert merged.source_ids == [SOURCE_IMAP, SOURCE_SEVENTEENTRACK_ACCOUNT]


async def test_zugestellte_sendung_wird_archiviert(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Zugestellt laut Konto: archivieren und danach nicht mehr liefern."""
    account.add(DHL_NUMBER, state=40, event=last_event("2026-07-30 11:05", "Zugestellt"))
    source = await make_source(hass)

    (shipment,) = await source.async_fetch()
    assert shipment.status is ShipmentStatus.DELIVERED
    assert shipment.delivered_at == local_time("2026-07-30 11:05")
    assert DHL_NUMBER in account.archived

    assert await source.async_fetch() == []
    assert source.diagnostics()["archived"] == 1


async def test_lokal_abgeschlossene_sendung_wird_archiviert(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Meldet die Mail die Zustellung zuerst, wird die Sendung archiviert, ohne Fragment."""
    account.add(DHL_NUMBER, event=last_event("2026-07-29 06:12", "Unterwegs"))
    source = await make_source(hass, [mail_shipment(status=ShipmentStatus.DELIVERED)])

    assert await source.async_fetch() == []
    assert DHL_NUMBER in account.archived


async def test_nummer_ohne_daten_liefert_nichts(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """„Not Found" ohne Ereignis haelt eine Sendung nicht kuenstlich frisch."""
    account.add(DHL_NUMBER, state=0)
    source = await make_source(hass)

    assert await source.async_fetch() == []


async def test_fehltreffer_werden_nicht_eingetragen(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Falsche Pruefziffer und generische Nummern ohne Pruefziffer bleiben draussen."""
    known = [
        mail_shipment("00340434161094042558"),
        mail_shipment("AB12345678XY", carrier="generic", status=ShipmentStatus.IN_TRANSIT),
    ]
    source = await make_source(hass, known)

    await source.async_fetch()
    assert "AddTrackNo" not in account.methods()


async def test_abgelehnte_nummer_wird_nicht_sofort_wiederholt(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount, freezer: FrozenDateTimeFactory
) -> None:
    """Eine Ablehnung gilt einen Tag lang; danach kommt die Nummer erneut dran."""
    account.invalid.add(DHL_NUMBER)
    source = await make_source(hass, [mail_shipment()])

    await source.async_fetch()
    await source.async_fetch()
    assert len(account.params("AddTrackNo")) == 1
    assert source.diagnostics()["rejected"] == 1

    freezer.tick(timedelta(days=1, seconds=1))
    await source.async_fetch()
    assert len(account.params("AddTrackNo")) == 2


async def test_hoechstens_zehn_neue_nummern_je_durchlauf(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Viele neue Nummern auf einmal verteilen sich auf mehrere Durchlaeufe."""
    known = [mail_shipment(f"JJD{index:018d}", carrier="dhl") for index in range(12)]
    source = await make_source(hass, known)
    # JJD-Nummern ohne Pruefziffer gelten bei DHL als plausibel, nicht als falsch.
    await source.async_fetch()
    first = len(account.params("AddTrackNo"))
    await source.async_fetch()

    assert first == 10
    assert len(account.params("AddTrackNo")) == 12


async def test_seitenweise_liste(hass: HomeAssistant, account: FakeSeventeenTrackAccount) -> None:
    """Mehr als 40 Sendungen kommen ueber mehrere Seiten."""
    for index in range(45):
        account.add(f"JJD{index:018d}", event=last_event("2026-07-29 06:12", "Unterwegs"))
    source = await make_source(hass)

    assert len(await source.async_fetch()) == 45
    assert [param["Page"] for param in account.params("GetTrackInfoList")] == [1, 2]


# ----------------------------------------------------------------------
# Fehlerbilder
# ----------------------------------------------------------------------


async def test_falsches_passwort(hass: HomeAssistant, account: FakeSeventeenTrackAccount) -> None:
    """Eine abgelehnte Anmeldung ist ein Fehler der Zugangsdaten."""
    with pytest.raises(SourceAuthError):
        await make_source(hass, password="falsch")


async def test_abgelaufene_sitzung_wird_erneuert(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Die Sitzung laeuft ab: einmal neu anmelden, dann geht es ohne Fehler weiter."""
    account.add(DHL_NUMBER, event=last_event("2026-07-29 06:12", "Unterwegs"))
    source = await make_source(hass)
    account.expire_session()

    assert len(await source.async_fetch()) == 1
    assert account.logins == 2


async def test_sitzung_laesst_sich_nicht_erneuern(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Wird das Passwort geaendert, waehrend die Quelle laeuft, ist das ein Reauth-Fall."""
    source = await make_source(hass)
    account.expire_session()
    account.password = "neu"

    with pytest.raises(SourceAuthError):
        await source.async_fetch()


async def test_drosselung_mit_backoff(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount, freezer: FrozenDateTimeFactory
) -> None:
    """HTTP 429: Pause ohne Anfragen, danach wieder normal."""
    source = await make_source(hass)
    account.throttle = 1

    with pytest.raises(SourceRateLimitError):
        await source.async_fetch()
    assert source.diagnostics()["backoff_seconds"] == 60

    account.calls.clear()
    with pytest.raises(SourceRateLimitError):
        await source.async_fetch()
    assert account.calls == []

    freezer.tick(timedelta(seconds=61))
    assert await source.async_fetch() == []
    assert source.diagnostics()["backoff_seconds"] == 0


async def test_dienst_kaputt(hass: HomeAssistant, account: FakeSeventeenTrackAccount) -> None:
    """HTTP 500 ist voruebergehend, kein Fehler der Zugangsdaten."""
    source = await make_source(hass)
    account.broken = True

    with pytest.raises(SourceError) as err:
        await source.async_fetch()
    assert not isinstance(err.value, SourceAuthError)


# ----------------------------------------------------------------------
# Config-Flow
# ----------------------------------------------------------------------


@pytest.fixture
async def hub(hass: HomeAssistant, account: FakeSeventeenTrackAccount) -> ConfigEntry:
    """Ein geladener Hub-Entry ohne Quellen."""
    return await setup_entry_without_sources(hass)


async def setup_entry_without_sources(hass: HomeAssistant) -> ConfigEntry:
    """Hub-Entry ohne Subentries laden."""
    entry = make_entry()
    await setup_entry(hass, entry)
    return entry


async def test_flow_bietet_beide_quellen_gekennzeichnet_an(
    hass: HomeAssistant, hub: ConfigEntry
) -> None:
    """Die Typauswahl kommt zuerst, mit dem kostenlosen Konto als Vorauswahl."""
    result = await start_subentry_flow(hass, hub, SUBENTRY_API_SOURCE)

    assert result["step_id"] == "user"
    selector = result["data_schema"].schema["source_type"]
    assert selector.config["options"] == ["seventeentrack_account", "seventeentrack"]
    (key,) = result["data_schema"].schema
    assert key.default() == "seventeentrack_account"


async def test_flow_legt_konto_an(
    hass: HomeAssistant, hub: ConfigEntry, account: FakeSeventeenTrackAccount
) -> None:
    """Falsches Passwort, Dienst kaputt, dann angenommen; ein zweites Konto gibt es nicht."""
    result = await start_subentry_flow(hass, hub, SUBENTRY_API_SOURCE)
    result = await configure_subentry(hass, result, {"source_type": "seventeentrack_account"})
    assert result["step_id"] == "account"

    form = {"username": f" {ACCOUNT_USERNAME} ", "password": "falsch", "scan_interval": 1800}
    result = await configure_subentry(hass, result, form)
    assert result["errors"] == {"base": "invalid_auth"}
    assert "falsch" not in str(result)

    account.broken = True
    result = await configure_subentry(hass, result, {**form, "password": ACCOUNT_PASSWORD})
    assert result["errors"] == {"base": "cannot_connect"}

    account.broken = False
    result = await configure_subentry(hass, result, {**form, "password": ACCOUNT_PASSWORD})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "17TRACK-Konto"
    (subentry,) = subentries_of(hub, SUBENTRY_API_SOURCE)
    assert subentry.data == {
        "source_type": "seventeentrack_account",
        "username": ACCOUNT_USERNAME,
        "password": ACCOUNT_PASSWORD,
        "scan_interval": 1800,
        "excluded_carriers": [],
    }

    result = await start_subentry_flow(hass, hub, SUBENTRY_API_SOURCE)
    result = await configure_subentry(hass, result, {"source_type": "seventeentrack_account"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_flow_konto_bearbeiten(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Leeres Passwort bleibt, die E-Mail ist vorausgefuellt, das Intervall aendert sich."""
    entry = make_entry(ACCOUNT_SOURCE)
    await setup_entry(hass, entry)
    (subentry,) = subentries_of(entry, SUBENTRY_API_SOURCE)

    result = await start_subentry_flow(hass, entry, SUBENTRY_API_SOURCE, subentry.subentry_id)
    assert result["step_id"] == "reconfigure"
    assert set(result["data_schema"].schema) == {
        "username",
        "password",
        "scan_interval",
        "tracked_carriers",
    }
    result = await configure_subentry(
        hass, result, {"username": ACCOUNT_USERNAME, "scan_interval": 7200}
    )

    assert result["reason"] == "reconfigure_successful"
    data = entry.subentries[subentry.subentry_id].data
    assert data["password"] == ACCOUNT_PASSWORD
    assert data["scan_interval"] == 7200


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


async def test_mail_und_konto_im_coordinator(
    hass: HomeAssistant,
    account: FakeSeventeenTrackAccount,
    imap_server: FakeImapServer,
    email_fixture: Callable[[str], bytes],
) -> None:
    """Mail gefunden → ins Konto eingetragen → Status uebernommen, Zugangsdaten bleiben privat."""
    imap_server.add_message(email_fixture("dhl/sendungsankuendigung.eml"))
    coordinator = await setup_entry(
        hass, make_entry(mailbox(imap_server), ACCOUNT_SOURCE, retention_days=0)
    )
    await wait_for(lambda: bool(coordinator.data.shipments))

    await coordinator.async_refresh()
    assert DHL_NUMBER in account.packages

    account.update(
        DHL_NUMBER,
        FPackageState=10,
        FLastEvent=last_event("2026-07-29 06:12", "Im Paketzentrum bearbeitet"),
    )
    await coordinator.async_refresh()

    (shipment,) = coordinator.data.shipments
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.sender == "Kaffeerösterei Nord"
    assert shipment.source_ids == [SOURCE_IMAP, SOURCE_SEVENTEENTRACK_ACCOUNT]

    diagnostics = await async_get_config_entry_diagnostics(hass, coordinator.config_entry)
    (handle,) = handle_by_kind(coordinator, SUBENTRY_API_SOURCE)
    assert diagnostics["sources"][handle.key]["api"]["added"] == 1
    assert ACCOUNT_PASSWORD not in str(diagnostics)
    assert ACCOUNT_USERNAME not in str(diagnostics)


async def test_falsches_passwort_fuehrt_zu_repair_und_reauth(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Eigener Repairs-Text fuer die Anmeldung; der Reauth fragt das Passwort ab."""
    account.password = "geaendert"
    entry = make_entry(ACCOUNT_SOURCE)
    coordinator = await setup_entry(hass, entry)
    await coordinator.async_refresh()
    (handle,) = handle_by_kind(coordinator, SUBENTRY_API_SOURCE)
    assert handle.state is SourceState.AUTH_FAILED

    issues = [
        issue.translation_key
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    ]
    assert issues == [ISSUE_API_LOGIN_INVALID]

    await hass.async_block_till_done()
    (flow,) = [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    assert set(result["data_schema"].schema) == {"password"}

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"password": "geaendert"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    (subentry,) = entry.get_subentries_of_type(SUBENTRY_API_SOURCE)
    assert subentry.data["password"] == "geaendert"
    assert entry.state is config_entries.ConfigEntryState.LOADED


def test_fabrik_ist_eingetragen() -> None:
    """Die Quelle traegt sich beim Import selbst als Fabrik fuer ihren Typ ein."""
    assert API_SOURCE_FACTORIES[SOURCE_SEVENTEENTRACK_ACCOUNT] is create_source


# ----------------------------------------------------------------------
# Zusteller-Filter
# ----------------------------------------------------------------------


async def test_abgewaehlter_zusteller_wird_nie_angefasst(
    hass: HomeAssistant, account: FakeSeventeenTrackAccount
) -> None:
    """Abgewaehlt: nicht eintragen, nicht uebernehmen, nicht archivieren — die anderen schon."""
    account.add(UPS_NUMBER, carrier=100002, event=last_event("2026-07-29 08:15", "Unterwegs"))
    account.add("00340434161094042557", state=40, event=last_event("2026-07-30 11:05", "Da"))
    context = SourceContext(
        known_shipments=lambda: [mail_shipment("JJD000000000000000001", carrier="dpd")],
        registry=await async_get_registry(hass),
    )
    source = create_source(
        hass,
        {
            "username": ACCOUNT_USERNAME,
            "password": ACCOUNT_PASSWORD,
            "excluded_carriers": ["ups", "dhl", "dpd"],
        },
        context,
    )
    await source.async_setup()

    assert await source.async_fetch() == []
    assert "AddTrackNo" not in account.methods()
    assert "SetTrackArchived" not in account.methods()
    assert source.diagnostics()["excluded_carriers"] == ["dhl", "dpd", "ups"]


async def test_flow_zusteller_auswaehlen_und_spaeter_aendern(
    hass: HomeAssistant, hub: ConfigEntry
) -> None:
    """Gespeichert wird, was abgewaehlt ist; beim Bearbeiten ist die Auswahl vorbelegt."""
    result = await start_subentry_flow(hass, hub, SUBENTRY_API_SOURCE)
    result = await configure_subentry(hass, result, {"source_type": "seventeentrack_account"})
    carriers = result["data_schema"].schema["tracked_carriers"].config["options"]
    all_ids = {option["value"] for option in carriers}
    assert {"amazon", "dpd", "generic"} <= all_ids

    result = await configure_subentry(
        hass,
        result,
        {
            "username": ACCOUNT_USERNAME,
            "password": ACCOUNT_PASSWORD,
            "scan_interval": 1800,
            "tracked_carriers": sorted(all_ids - {"amazon"}),
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = subentries_of(hub, SUBENTRY_API_SOURCE)
    assert subentry.data["excluded_carriers"] == ["amazon"]

    result = await start_subentry_flow(hass, hub, SUBENTRY_API_SOURCE, subentry.subentry_id)
    (key,) = [key for key in result["data_schema"].schema if key == "tracked_carriers"]
    assert "amazon" not in key.default()
    assert "dpd" in key.default()

    result = await configure_subentry(
        hass,
        result,
        {
            "username": ACCOUNT_USERNAME,
            "scan_interval": 1800,
            "tracked_carriers": sorted(all_ids - {"amazon", "hermes"}),
        },
    )
    assert result["reason"] == "reconfigure_successful"
    assert hub.subentries[subentry.subentry_id].data["excluded_carriers"] == ["amazon", "hermes"]
