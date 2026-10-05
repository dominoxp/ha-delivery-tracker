"""Tests des Config-Flows: Hub-Entry, Subentries und Options."""

import asyncio
import contextlib
import socket
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delivery_tracker.const import (
    DESTINATION_UNKNOWN,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_API_SOURCE,
    SUBENTRY_DESTINATION,
    SUBENTRY_MAILBOX,
)
from custom_components.delivery_tracker.entry import destination_device_identifier
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus
from tests.fake_imap import FakeImapServer

VALID_API_KEY = "0123456789ABCDEF0123456789ABCDEF"

ZUHAUSE = ConfigSubentryData(
    data={
        "id": "zuhause",
        "name": "Zuhause",
        "streets": ["Musterstraße 1"],
        "postal_codes": ["12345"],
    },
    subentry_type=SUBENTRY_DESTINATION,
    title="Zuhause",
    unique_id="zuhause",
)


# ----------------------------------------------------------------------
# Fixtures und Hilfen
# ----------------------------------------------------------------------


@pytest.fixture
async def imap_server(socket_enabled: None) -> AsyncGenerator[FakeImapServer]:
    """Fake-IMAP-Server mit einem zusaetzlichen Ordner fuer Paketmails."""
    server = FakeImapServer(folders=("INBOX", "Pakete"), noselect_folders=("Archiv",))
    await server.start()
    yield server
    await server.stop()


def mailbox_input(server: FakeImapServer, **overrides: Any) -> dict[str, Any]:
    """Formulareingabe des ersten Postfach-Schritts fuer den Fake-Server."""
    values: dict[str, Any] = {
        "server": "127.0.0.1",
        "port": server.port,
        "security": "none",
        "verify_ssl": False,
        "username": server.username,
        "password": server.password,
    }
    values.update(overrides)
    return values


FOLDER_INPUT = {"folder": "Pakete", "use_idle": True, "poll_interval": 120, "lookback_days": 5}


def unused_port() -> int:
    """Ein Port auf 127.0.0.1, an dem garantiert niemand lauscht."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def start_subentry_flow(
    hass: HomeAssistant, entry: ConfigEntry, subentry_type: str, subentry_id: str | None = None
) -> Any:
    """Einen Subentry-Flow starten — neu oder als Bearbeitung."""
    context: dict[str, Any] = {"source": config_entries.SOURCE_USER}
    if subentry_id is not None:
        context = {"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": subentry_id}
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, subentry_type), context=context
    )


async def configure_subentry(hass: HomeAssistant, result: Any, user_input: dict[str, Any]) -> Any:
    """Einen Schritt eines Subentry-Flows ausfuellen und auf Reloads warten."""
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


def subentries_of(entry: ConfigEntry, subentry_type: str) -> list[Any]:
    """Alle Subentries eines Typs."""
    return entry.get_subentries_of_type(subentry_type)


@contextlib.contextmanager
def count_reloads(hass: HomeAssistant) -> Any:
    """Zaehlen, wie oft ein Entry neu geladen wird."""
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as mock:
        yield mock


# ----------------------------------------------------------------------
# Hub-Entry
# ----------------------------------------------------------------------


async def test_user_flow_legt_entry_an(hass: HomeAssistant) -> None:
    """Der Benutzer-Flow erzeugt einen Entry mit Namen und Standard-Options."""
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
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.options["retention_days"] == 14
    assert entry.options["stale_days"] == 60
    assert entry.options["generic_fallback"] is True
    assert entry.options["disabled_carriers"] == []


async def test_nur_eine_instanz(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Ein zweiter Entry wird abgelehnt."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_vollstaendiger_ablauf_aller_subentry_typen(
    hass: HomeAssistant, setup_integration: ConfigEntry, imap_server: FakeImapServer
) -> None:
    """Je ein Subentry aller drei Typen anlegen, eines bearbeiten, eines loeschen."""
    entry = setup_integration

    result = await start_subentry_flow(hass, entry, SUBENTRY_MAILBOX)
    result = await configure_subentry(hass, result, mailbox_input(imap_server))
    result = await configure_subentry(hass, result, FOLDER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await start_subentry_flow(hass, entry, SUBENTRY_DESTINATION)
    result = await configure_subentry(hass, result, {"name": "Büro", "postal_codes": ["54321"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await start_subentry_flow(hass, entry, SUBENTRY_API_SOURCE)
    result = await configure_subentry(
        hass,
        result,
        {"source_type": "seventeentrack", "api_key": VALID_API_KEY, "scan_interval": 1800},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert len(entry.subentries) == 3

    (office,) = subentries_of(entry, SUBENTRY_DESTINATION)
    result = await start_subentry_flow(hass, entry, SUBENTRY_DESTINATION, office.subentry_id)
    result = await configure_subentry(
        hass, result, {"name": "Büro", "postal_codes": ["54321"], "cities": ["Arbeitsstadt"]}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[office.subentry_id].data["cities"] == ["Arbeitsstadt"]

    (api,) = subentries_of(entry, SUBENTRY_API_SOURCE)
    assert hass.config_entries.async_remove_subentry(entry, api.subentry_id)
    await hass.async_block_till_done()

    assert {subentry.subentry_type for subentry in entry.subentries.values()} == {
        SUBENTRY_MAILBOX,
        SUBENTRY_DESTINATION,
    }
    assert entry.state is config_entries.ConfigEntryState.LOADED


# ----------------------------------------------------------------------
# Subentry: Postfach
# ----------------------------------------------------------------------


async def test_mailbox_verbindungstest_und_ordnerauswahl(
    hass: HomeAssistant, setup_integration: ConfigEntry, imap_server: FakeImapServer
) -> None:
    """Nach erfolgreicher Anmeldung stehen die Ordner des Servers zur Auswahl."""
    entry = setup_integration

    result = await start_subentry_flow(hass, entry, SUBENTRY_MAILBOX)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await configure_subentry(hass, result, mailbox_input(imap_server))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "folder"
    folder_selector = result["data_schema"].schema["folder"]
    offered = [option["value"] for option in folder_selector.config["options"]]
    assert offered == ["INBOX", "Pakete"]

    result = await configure_subentry(hass, result, FOLDER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"{imap_server.username}@127.0.0.1 (Pakete)"

    (mailbox,) = subentries_of(entry, SUBENTRY_MAILBOX)
    assert mailbox.data["folder"] == "Pakete"
    assert mailbox.data["port"] == imap_server.port
    assert mailbox.data["password"] == imap_server.password
    assert mailbox.data["poll_interval"] == 120


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"password": "falsch"}, "invalid_auth"),
        ({"username": "jemand"}, "invalid_auth"),
        ({"port": "unused"}, "cannot_connect"),
    ],
)
async def test_mailbox_fehler_bei_der_anmeldung(
    hass: HomeAssistant,
    setup_integration: ConfigEntry,
    imap_server: FakeImapServer,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, Any],
    error: str,
) -> None:
    """Falsche Zugangsdaten und ein unerreichbarer Server haben eigene Meldungen."""
    if overrides.get("port") == "unused":
        overrides = {"port": unused_port()}

    result = await start_subentry_flow(hass, setup_integration, SUBENTRY_MAILBOX)
    result = await configure_subentry(hass, result, mailbox_input(imap_server, **overrides))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}
    # Das Passwort taucht weder in der Antwort noch im Log auf.
    assert "falsch" not in str(result)
    assert "falsch" not in caplog.text
    assert imap_server.password not in caplog.text


async def test_mailbox_fehlender_ordner(
    hass: HomeAssistant, setup_integration: ConfigEntry, imap_server: FakeImapServer
) -> None:
    """Ein frei eingegebener, nicht existierender Ordner wird abgewiesen."""
    result = await start_subentry_flow(hass, setup_integration, SUBENTRY_MAILBOX)
    result = await configure_subentry(hass, result, mailbox_input(imap_server))

    result = await configure_subentry(hass, result, {**FOLDER_INPUT, "folder": "Gibtsnicht"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "folder"
    assert result["errors"] == {"base": "folder_not_found"}

    # Ein reiner Elternordner (\Noselect) taucht nicht in der Auswahl auf und laesst
    # sich auch frei eingegeben nicht oeffnen.
    result = await configure_subentry(hass, result, {**FOLDER_INPUT, "folder": "Archiv"})
    assert result["errors"] == {"base": "folder_not_found"}

    result = await configure_subentry(hass, result, FOLDER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_mailbox_verbindungstest_haengt_nicht(
    hass: HomeAssistant, setup_integration: ConfigEntry, socket_enabled: None
) -> None:
    """Ein Server, der nie antwortet, fuehrt nach dem Zeitlimit zu einer Fehlermeldung."""
    connections: list[asyncio.StreamWriter] = []
    release = asyncio.Event()

    async def _silent(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Verbindung annehmen, aber nie begruessen.
        connections.append(writer)
        await release.wait()

    server = await asyncio.start_server(_silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with patch("custom_components.delivery_tracker.config_flow.CONNECTION_TEST_TIMEOUT", 0.3):
            result = await start_subentry_flow(hass, setup_integration, SUBENTRY_MAILBOX)
            async with asyncio.timeout(5):
                result = await configure_subentry(
                    hass,
                    result,
                    {
                        "server": "127.0.0.1",
                        "port": port,
                        "security": "none",
                        "verify_ssl": False,
                        "username": "max",
                        "password": "geheim",
                    },
                )
    finally:
        release.set()
        for writer in connections:
            writer.close()
        server.close()
        await server.wait_closed()

    assert result["errors"] == {"base": "cannot_connect"}


async def test_mailbox_doppelt_wird_abgewiesen(
    hass: HomeAssistant, setup_integration: ConfigEntry, imap_server: FakeImapServer
) -> None:
    """Dasselbe Postfach mit demselben Ordner gibt es nur einmal."""
    for _ in range(2):
        result = await start_subentry_flow(hass, setup_integration, SUBENTRY_MAILBOX)
        result = await configure_subentry(hass, result, mailbox_input(imap_server))
        result = await configure_subentry(hass, result, FOLDER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(subentries_of(setup_integration, SUBENTRY_MAILBOX)) == 1


async def test_mailbox_bearbeiten_behaelt_leeres_passwort(
    hass: HomeAssistant, setup_integration: ConfigEntry, imap_server: FakeImapServer
) -> None:
    """Beim Bearbeiten bleibt das gespeicherte Passwort, wenn das Feld leer bleibt."""
    entry = setup_integration
    result = await start_subentry_flow(hass, entry, SUBENTRY_MAILBOX)
    result = await configure_subentry(hass, result, mailbox_input(imap_server))
    result = await configure_subentry(hass, result, FOLDER_INPUT)
    (mailbox,) = subentries_of(entry, SUBENTRY_MAILBOX)

    result = await start_subentry_flow(hass, entry, SUBENTRY_MAILBOX, mailbox.subentry_id)
    assert result["step_id"] == "reconfigure"
    # Das Passwort wird nie ins Formular zurueckgeschickt.
    assert imap_server.password not in str(result["data_schema"].schema)

    connection = mailbox_input(imap_server)
    del connection["password"]
    result = await configure_subentry(hass, result, connection)
    assert result["step_id"] == "folder"
    result = await configure_subentry(hass, result, {**FOLDER_INPUT, "folder": "INBOX"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[mailbox.subentry_id]
    assert updated.data["password"] == imap_server.password
    assert updated.data["folder"] == "INBOX"
    assert updated.title == f"{imap_server.username}@127.0.0.1"


# ----------------------------------------------------------------------
# Subentry: Zielort
# ----------------------------------------------------------------------


async def test_destination_bekommt_id_und_device(
    hass: HomeAssistant, setup_integration: ConfigEntry
) -> None:
    """Die ID entsteht aus dem Name, jede Adresse bekommt ein eigenes Device."""
    entry = setup_integration
    result = await start_subentry_flow(hass, entry, SUBENTRY_DESTINATION)
    result = await configure_subentry(
        hass,
        result,
        {"name": "Büro", "streets": ["Hauptstr. 5", "  ", "Hauptstr. 5"], "postal_codes": []},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (office,) = subentries_of(entry, SUBENTRY_DESTINATION)
    assert office.data["id"] == "buro"
    assert office.unique_id == "buro"
    assert office.data["streets"] == ["Hauptstr. 5"]
    assert office.data["postal_codes"] == []

    device = dr.async_get(hass).async_get_device(
        identifiers={destination_device_identifier(entry, "buro")}
    )
    assert device is not None
    assert device.name == "Büro"
    assert office.subentry_id in device.config_entries_subentries[entry.entry_id]


@pytest.mark.parametrize(
    ("user_input", "errors"),
    [
        ({"name": "Unbekannt", "cities": ["X"]}, {"name": "invalid_name"}),
        ({"name": "unknown", "cities": ["X"]}, {"name": "invalid_name"}),
        ({"name": "   ", "cities": ["X"]}, {"name": "invalid_name"}),
        ({"name": "Zuhause", "cities": ["X"]}, {"name": "name_exists"}),
        ({"name": "Garage", "streets": ["", " "]}, {"base": "no_rules"}),
    ],
)
async def test_destination_eingaben_werden_geprueft(
    hass: HomeAssistant,
    user_input: dict[str, Any],
    errors: dict[str, str],
) -> None:
    """Reservierte, doppelte und regellose Zielorte werden abgewiesen."""
    entry = MockConfigEntry(domain=DOMAIN, title="Delivery Tracker", subentries_data=[ZUHAUSE])
    entry.add_to_hass(hass)

    result = await start_subentry_flow(hass, entry, SUBENTRY_DESTINATION)
    result = await configure_subentry(hass, result, user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors


async def test_destination_umbenennen_behaelt_id(hass: HomeAssistant) -> None:
    """Eine Umbenennung aendert Titel und Device-Name, aber nicht die ID."""
    entry = MockConfigEntry(domain=DOMAIN, title="Delivery Tracker", subentries_data=[ZUHAUSE])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (home,) = subentries_of(entry, SUBENTRY_DESTINATION)

    result = await start_subentry_flow(hass, entry, SUBENTRY_DESTINATION, home.subentry_id)
    assert result["step_id"] == "reconfigure"
    result = await configure_subentry(
        hass, result, {"name": "Wohnung", "streets": ["Musterstraße 1"]}
    )

    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[home.subentry_id]
    assert updated.title == "Wohnung"
    assert updated.data["id"] == "zuhause"
    assert updated.data["postal_codes"] == []
    device = dr.async_get(hass).async_get_device(
        identifiers={destination_device_identifier(entry, "zuhause")}
    )
    assert device is not None
    assert device.name == "Wohnung"


async def test_destination_loeschen_ordnet_sendungen_unbekannt_zu(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Die Sendungen eines geloeschten Zielorts ziehen nach „Unbekannt" um."""
    shipment = Shipment(
        id="dhl_00340434161094042557",
        carrier="dhl",
        tracking_number="00340434161094042557",
        status=ShipmentStatus.IN_TRANSIT,
        destination_id="zuhause",
        destination_raw="Max Mustermann, Musterstraße 1, 12345 Musterstadt",
    )
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"shipments": [shipment.to_dict()]},
    }
    entry = MockConfigEntry(domain=DOMAIN, title="Delivery Tracker", subentries_data=[ZUHAUSE])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    stored = entry.runtime_data.store.get(shipment.id)
    assert stored is not None
    assert stored.destination_id == "zuhause"
    device_registry = dr.async_get(hass)
    identifier = destination_device_identifier(entry, "zuhause")
    assert device_registry.async_get_device(identifiers={identifier}) is not None

    (home,) = subentries_of(entry, SUBENTRY_DESTINATION)
    assert hass.config_entries.async_remove_subentry(entry, home.subentry_id)
    await hass.async_block_till_done()

    moved = entry.runtime_data.store.get(shipment.id)
    assert moved is not None
    assert moved.destination_id == DESTINATION_UNKNOWN
    assert moved.destination_raw == shipment.destination_raw
    assert device_registry.async_get_device(identifiers={identifier}) is None


# ----------------------------------------------------------------------
# Subentry: API-Quelle
# ----------------------------------------------------------------------


async def test_api_source_schluessel_wird_geprueft(
    hass: HomeAssistant, setup_integration: ConfigEntry
) -> None:
    """Ein offensichtlich ungueltiger Schluessel wird abgewiesen, ein zweiter Eintrag auch."""
    entry = setup_integration
    result = await start_subentry_flow(hass, entry, SUBENTRY_API_SOURCE)
    result = await configure_subentry(
        hass,
        result,
        {"source_type": "seventeentrack", "api_key": "zu kurz", "scan_interval": 1800},
    )
    assert result["errors"] == {"base": "invalid_api_key"}
    assert "zu kurz" not in str(result)

    result = await configure_subentry(
        hass,
        result,
        {"source_type": "seventeentrack", "api_key": f" {VALID_API_KEY} ", "scan_interval": 1800},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "17TRACK"
    (api,) = subentries_of(entry, SUBENTRY_API_SOURCE)
    assert api.data == {
        "source_type": "seventeentrack",
        "api_key": VALID_API_KEY,
        "scan_interval": 1800,
    }

    result = await start_subentry_flow(hass, entry, SUBENTRY_API_SOURCE)
    result = await configure_subentry(
        hass,
        result,
        {"source_type": "seventeentrack", "api_key": VALID_API_KEY, "scan_interval": 1800},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_api_source_bearbeiten(hass: HomeAssistant, setup_integration: ConfigEntry) -> None:
    """Leerer Schluessel bleibt, das Intervall aendert sich."""
    entry = setup_integration
    result = await start_subentry_flow(hass, entry, SUBENTRY_API_SOURCE)
    await configure_subentry(
        hass,
        result,
        {"source_type": "seventeentrack", "api_key": VALID_API_KEY, "scan_interval": 1800},
    )
    (api,) = subentries_of(entry, SUBENTRY_API_SOURCE)

    result = await start_subentry_flow(hass, entry, SUBENTRY_API_SOURCE, api.subentry_id)
    assert result["step_id"] == "reconfigure"
    result = await configure_subentry(hass, result, {"scan_interval": 7200})

    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[api.subentry_id].data["api_key"] == VALID_API_KEY
    assert entry.subentries[api.subentry_id].data["scan_interval"] == 7200


# ----------------------------------------------------------------------
# Options und Reload
# ----------------------------------------------------------------------


async def test_options_flow(hass: HomeAssistant, setup_integration: ConfigEntry) -> None:
    """Abgeschaltete Zusteller werden als Negativliste gespeichert."""
    entry = setup_integration
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    offered = {
        option["value"]
        for option in result["data_schema"].schema["enabled_carriers"].config["options"]
    }
    assert {"dhl", "amazon", "dpd", "gls", "hermes", "ups"} <= offered
    assert "generic" not in offered

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "retention_days": 7,
            "stale_days": 30,
            "enabled_carriers": sorted(offered - {"amazon", "ups"}),
            "generic_fallback": False,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "retention_days": 7,
        "stale_days": 30,
        "disabled_carriers": ["amazon", "ups"],
        "generic_fallback": False,
    }
    assert entry.runtime_data.store.retention_days == 7


async def test_jede_aenderung_loest_genau_einen_reload_aus(
    hass: HomeAssistant, setup_integration: ConfigEntry
) -> None:
    """Options, neuer, geaenderter und geloeschter Subentry: je ein Reload."""
    entry = setup_integration

    with count_reloads(hass) as reloads:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "retention_days": 3,
                "stale_days": 60,
                "enabled_carriers": ["dhl"],
                "generic_fallback": True,
            },
        )
        await hass.async_block_till_done()
    assert reloads.call_count == 1

    with count_reloads(hass) as reloads:
        result = await start_subentry_flow(hass, entry, SUBENTRY_DESTINATION)
        await configure_subentry(hass, result, {"name": "Büro", "cities": ["Arbeitsstadt"]})
    assert reloads.call_count == 1

    (office,) = subentries_of(entry, SUBENTRY_DESTINATION)
    with count_reloads(hass) as reloads:
        result = await start_subentry_flow(hass, entry, SUBENTRY_DESTINATION, office.subentry_id)
        await configure_subentry(hass, result, {"name": "Büro", "cities": ["Neustadt"]})
    assert reloads.call_count == 1

    with count_reloads(hass) as reloads:
        hass.config_entries.async_remove_subentry(entry, office.subentry_id)
        await hass.async_block_till_done()
    assert reloads.call_count == 1

    assert entry.state is config_entries.ConfigEntryState.LOADED
