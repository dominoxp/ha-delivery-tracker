"""Repairs-Eintraege fuer Fehler, die der Nutzer selbst beheben muss."""

from collections.abc import AsyncGenerator, Mapping
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.delivery_tracker.const import (
    DOMAIN,
    ISSUE_API_KEY_INVALID,
    ISSUE_API_QUOTA_EXHAUSTED,
    ISSUE_MAILBOX_AUTH_FAILED,
    ISSUE_MAILBOX_FOLDER_NOT_FOUND,
    SOURCE_SEVENTEENTRACK,
    SUBENTRY_API_SOURCE,
    SUBENTRY_MAILBOX,
)
from custom_components.delivery_tracker.coordinator import SourceState
from custom_components.delivery_tracker.issues import source_issue_id
from custom_components.delivery_tracker.sources import (
    API_SOURCE_FACTORIES,
    ImapSource,
    SourceAuthError,
    SourceQuotaError,
    TrackingSource,
)
from tests.fake_imap import FakeImapServer
from tests.test_coordinator import (
    API_SOURCE,
    FakeApiSource,
    handle_by_kind,
    mailbox,
    make_entry,
    setup_entry,
    wait_for,
)


@pytest.fixture(autouse=True)
def enable_sockets(socket_enabled: None) -> None:
    """Die Postfaecher sprechen ueber einen echten Socket auf 127.0.0.1."""


@pytest.fixture
async def imap_server() -> AsyncGenerator[FakeImapServer]:
    """Fake-Server mit einem Ordner."""
    server = FakeImapServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def api_sources() -> Any:
    """17track durch das Fake ersetzen; liefert die erzeugten Quellen."""
    created: list[FakeApiSource] = []

    def _factory(hass: HomeAssistant, data: Mapping[str, Any], context: Any) -> TrackingSource:
        source = FakeApiSource()
        created.append(source)
        return source

    with patch.dict(API_SOURCE_FACTORIES, {SOURCE_SEVENTEENTRACK: _factory}):
        yield created


def issue(hass: HomeAssistant, subentry_id: str, key: str) -> ir.IssueEntry | None:
    """Den Eintrag einer Quelle holen, falls es ihn gibt."""
    return ir.async_get(hass).async_get_issue(DOMAIN, source_issue_id(subentry_id, key))


def own_issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    """Alle Eintraege dieser Integration."""
    return [item for (domain, _), item in ir.async_get(hass).issues.items() if domain == DOMAIN]


async def test_abgelehnte_anmeldung_bis_zur_neukonfiguration(
    hass: HomeAssistant, imap_server: FakeImapServer
) -> None:
    """Falsches Passwort erzeugt einen Eintrag; nach der Neukonfiguration ist er weg."""
    entry = make_entry(mailbox(imap_server, password="falsch"))
    coordinator = await setup_entry(hass, entry)
    (handle,) = handle_by_kind(coordinator, SUBENTRY_MAILBOX)

    await wait_for(lambda: handle.state is SourceState.AUTH_FAILED)
    await coordinator.async_refresh()

    found = issue(hass, handle.key, ISSUE_MAILBOX_AUTH_FAILED)
    assert found is not None
    assert found.severity is ir.IssueSeverity.ERROR
    assert found.is_fixable is False
    assert found.translation_placeholders == {"source": handle.title}

    # Neukonfiguration des Postfachs wie in der Oberflaeche: Zugangsdaten, dann Ordner.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_MAILBOX),
        context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": handle.key},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "server": "127.0.0.1",
            "port": imap_server.port,
            "security": "none",
            "verify_ssl": False,
            "username": imap_server.username,
            "password": imap_server.password,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"folder": "INBOX", "use_idle": True, "poll_interval": 3600, "lookback_days": 3},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    reloaded = entry.runtime_data.coordinator
    (handle,) = handle_by_kind(reloaded, SUBENTRY_MAILBOX)
    await wait_for(lambda: isinstance(handle.source, ImapSource) and handle.source.available)
    await reloaded.async_refresh()

    assert handle.state is SourceState.OK
    assert issue(hass, handle.key, ISSUE_MAILBOX_AUTH_FAILED) is None


async def test_verschwundener_ordner(hass: HomeAssistant, imap_server: FakeImapServer) -> None:
    """Ein Ordner, den es nicht gibt, ist ein eigener Eintrag — kein Reauth."""
    coordinator = await setup_entry(hass, make_entry(mailbox(imap_server, folder="Weg")))
    (handle,) = handle_by_kind(coordinator, SUBENTRY_MAILBOX)

    await wait_for(lambda: handle.state is SourceState.FAILED)
    await coordinator.async_refresh()

    assert issue(hass, handle.key, ISSUE_MAILBOX_FOLDER_NOT_FOUND) is not None
    assert issue(hass, handle.key, ISSUE_MAILBOX_AUTH_FAILED) is None


@pytest.mark.parametrize(
    ("error", "key", "severity"),
    [
        (SourceAuthError("Schluessel abgelaufen"), ISSUE_API_KEY_INVALID, ir.IssueSeverity.ERROR),
        (SourceQuotaError("Kontingent leer"), ISSUE_API_QUOTA_EXHAUSTED, ir.IssueSeverity.WARNING),
    ],
)
async def test_api_schluessel_bis_zum_naechsten_erfolg(
    hass: HomeAssistant,
    api_sources: list[FakeApiSource],
    error: Exception,
    key: str,
    severity: ir.IssueSeverity,
) -> None:
    """Abgelaufener oder aufgebrauchter Schluessel; nach dem Reload und Erfolg ist er weg."""
    entry = make_entry(API_SOURCE)
    coordinator = await setup_entry(hass, entry)
    (handle,) = handle_by_kind(coordinator, SUBENTRY_API_SOURCE)
    api_sources[0].error = error

    await coordinator.async_refresh()

    found = issue(hass, handle.key, key)
    assert found is not None
    assert found.severity is severity

    # Nach dem Reload steht der Ausgang erst mit dem ersten Abruf fest.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = entry.runtime_data.coordinator
    await reloaded.async_refresh()

    assert handle_by_kind(reloaded, SUBENTRY_API_SOURCE)[0].state is SourceState.OK
    assert issue(hass, handle.key, key) is None


async def test_voruebergehender_fehler_ist_kein_repair(
    hass: HomeAssistant, api_sources: list[FakeApiSource]
) -> None:
    """Ein nicht erreichbarer Dienst ist Normalbetrieb, kein Fall fuer den Nutzer."""
    coordinator = await setup_entry(hass, make_entry(API_SOURCE))
    api_sources[0].error = TimeoutError()

    await coordinator.async_refresh()

    (handle,) = handle_by_kind(coordinator, SUBENTRY_API_SOURCE)
    assert handle.state is SourceState.ERROR
    assert own_issues(hass) == []


async def test_loeschen_des_entries_raeumt_eintraege_ab(
    hass: HomeAssistant, api_sources: list[FakeApiSource]
) -> None:
    """Ohne Entry bleibt kein verwaister Hinweis stehen."""
    entry = make_entry(API_SOURCE)
    coordinator = await setup_entry(hass, entry)
    api_sources[0].error = SourceAuthError("abgelehnt")
    await coordinator.async_refresh()
    assert len(own_issues(hass)) == 1

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert own_issues(hass) == []
