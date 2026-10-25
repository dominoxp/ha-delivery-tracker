"""Tests der IMAP-Quelle gegen den echten Fake-Server aus `fake_imap`."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.models import Shipment, ShipmentStatus
from custom_components.delivery_tracker.sources import (
    ImapAuthError,
    ImapConfig,
    ImapConnectionError,
    ImapFolderError,
    ImapMode,
    ImapProgress,
    ImapSecurity,
    ImapSource,
    ImapState,
    RawMail,
)
from custom_components.delivery_tracker.sources.imap_source import _without_read_state
from tests.fake_imap import FakeImapServer, build_message, server_ssl_context

# Kurze Zeiten: Die Tests sollen an Zustaenden haengen, nicht an Uhren.
TIMEOUT = 5.0


class RecordingHandler:
    """Merkt sich jede Rohmail und macht eine Sendung daraus."""

    def __init__(self, *, fail_for: set[int] | None = None) -> None:
        """Handler anlegen; `fail_for` laesst die genannten UIDs scheitern."""
        self.mails: list[RawMail] = []
        self._fail_for = fail_for or set()

    def __call__(self, mail: RawMail) -> list[Shipment]:
        """Eine Mail „verarbeiten"."""
        if mail.uid in self._fail_for:
            raise ValueError(f"UID {mail.uid} ist kaputt")
        self.mails.append(mail)
        return [
            Shipment(
                id=f"fake_{mail.uid}",
                carrier="fake",
                tracking_number=str(mail.uid),
                status=ShipmentStatus.IN_TRANSIT,
            )
        ]

    @property
    def uids(self) -> list[int]:
        """UIDs aller verarbeiteten Mails, in Reihenfolge."""
        return [mail.uid for mail in self.mails]


@pytest.fixture(autouse=True)
def enable_sockets(socket_enabled: None) -> None:
    """Diese Tests sprechen ueber einen echten Socket auf 127.0.0.1."""


@pytest.fixture
async def server() -> AsyncGenerator[FakeImapServer]:
    """Ein laufender Fake-Server mit IDLE-Capability."""
    instance = FakeImapServer()
    await instance.start()
    yield instance
    await instance.stop()


def make_config(server: FakeImapServer, **overrides: object) -> ImapConfig:
    """Eine Konfiguration, die auf den Fake-Server zeigt."""
    values: dict[str, object] = {
        "server": "127.0.0.1",
        "port": server.port,
        "username": server.username,
        "password": server.password,
        "security": ImapSecurity.NONE,
        "command_timeout": 2.0,
        "poll_interval": 0.05,
        "backoff_start": 0.05,
        "backoff_max": 0.1,
    }
    values.update(overrides)
    return ImapConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
async def make_source() -> AsyncGenerator[Callable[..., ImapSource]]:
    """Quellen bauen und am Testende zuverlaessig schliessen."""
    created: list[ImapSource] = []

    def _factory(server: FakeImapServer, **kwargs: object) -> ImapSource:
        source_kwargs = {
            key: kwargs.pop(key) for key in ("handler", "progress", "on_new_data") if key in kwargs
        }
        source = ImapSource(make_config(server, **kwargs), **source_kwargs)  # type: ignore[arg-type]
        created.append(source)
        return source

    yield _factory

    for source in created:
        await source.async_close()


async def wait_for(condition: Callable[[], bool], *, seconds: float = TIMEOUT) -> None:
    """Warten, bis eine Bedingung eintritt — statt fester Schlafzeiten."""

    async def _poll() -> None:
        # Abgefragt wird der Zustand des Fake-Servers; ein Event gibt es dort nicht.
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    async with asyncio.timeout(seconds):
        await _poll()


async def test_fetch_reads_unseen_marks_seen_and_skips_them_next_round(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Grundfall: anmelden, ungelesene Mails holen, markieren, nicht erneut holen."""
    server.add_message(build_message(subject="Paket 1"))
    server.add_message(build_message(subject="Paket 2"))
    handler = RecordingHandler()
    source = make_source(server, handler=handler)

    await source.async_setup()
    shipments = await source.async_fetch()

    assert [shipment.tracking_number for shipment in shipments] == ["1", "2"]
    assert handler.uids == [1, 2]
    assert all(message.seen for message in server.mailbox().messages)
    assert source.state is ImapState.CONNECTED
    assert source.mode is ImapMode.PUSH

    fetched_before = list(server.fetched_uids)
    assert await source.async_fetch() == []
    assert server.fetched_uids == fetched_before


async def test_idle_push_triggers_fetch_without_polling(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Eine neue Mail im IDLE loest den Abruf aus, ohne auf das Intervall zu warten."""
    notified = asyncio.Event()
    handler = RecordingHandler()
    source = make_source(
        server,
        # Ein Polling-Intervall, das im Test nie ablaeuft: Was passiert, ist Push.
        poll_interval=3600,
        handler=handler,
        on_new_data=notified.set,
    )

    await source.async_setup()
    await source.async_start()
    await wait_for(lambda: server.idle_starts > 0)

    server.add_message(build_message(subject="Frisch eingetroffen"))
    async with asyncio.timeout(TIMEOUT):
        await notified.wait()

    shipments = await source.async_fetch()
    assert [shipment.tracking_number for shipment in shipments] == ["1"]
    assert source.mode is ImapMode.PUSH


async def test_server_without_idle_falls_back_to_polling(
    make_source: Callable[..., ImapSource],
) -> None:
    """Ohne IDLE-Capability liest die Quelle im Intervall."""
    server = FakeImapServer(capabilities=("IMAP4rev1",))
    await server.start()
    try:
        handler = RecordingHandler()
        source = make_source(server, poll_interval=0.02, handler=handler)

        await source.async_setup()
        assert source.mode is ImapMode.POLLING

        await source.async_start()
        server.add_message(build_message(subject="Kommt per Polling"))
        await wait_for(lambda: handler.uids == [1])
        assert server.idle_starts == 0

        await source.async_close()
    finally:
        await server.stop()


async def test_connection_drop_reconnects_without_loss_or_duplicates(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Ein Abriss mitten im Betrieb kostet weder eine Mail noch erzeugt er Dubletten."""
    notified = asyncio.Event()
    handler = RecordingHandler()
    source = make_source(server, poll_interval=3600, handler=handler, on_new_data=notified.set)

    await source.async_setup()
    await source.async_start()
    server.add_message(build_message(subject="Vor dem Abriss"))
    async with asyncio.timeout(TIMEOUT):
        await notified.wait()

    notified.clear()
    idle_starts = server.idle_starts
    assert server.drop_connections() == 1
    # Erst zustellen, wenn der Client wieder im IDLE haengt: Eine offene Verbindung
    # allein heisst nicht, dass er schon nachgelesen hat — eine Mail zwischen seiner
    # Suche und dem IDLE meldet der Fake-Server nie, der Test liefe in den Timeout.
    await wait_for(lambda: server.connections == 1 and server.idle_starts > idle_starts)

    server.add_message(build_message(subject="Nach dem Abriss"))
    async with asyncio.timeout(TIMEOUT):
        await notified.wait()

    assert handler.uids == [1, 2]
    assert source.state is ImapState.CONNECTED
    shipments = await source.async_fetch()
    assert [shipment.tracking_number for shipment in shipments] == ["1", "2"]


async def test_changed_uid_validity_resets_progress(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Neue `UIDVALIDITY` heisst: alle gemerkten UIDs sind wertlos."""
    server.add_message(build_message(subject="Alte Welt"))
    handler = RecordingHandler()
    source = make_source(server, handler=handler)

    await source.async_setup()
    await source.async_fetch()
    assert source.progress.last_uid == 1
    assert source.progress.uid_validity == 1000

    # Der Ordner wird neu aufgesetzt: gleiche UIDs, voellig andere Mails.
    await source.async_close()
    server.mailbox().reset(uid_validity=2000)
    server.add_message(build_message(subject="Neue Welt"))

    await source.async_setup()
    shipments = await source.async_fetch()

    assert source.progress.uid_validity == 2000
    assert [shipment.tracking_number for shipment in shipments] == ["1"]
    assert handler.uids == [1, 1]


async def test_wrong_password_is_a_permanent_error(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Falsches Passwort ist klar unterscheidbar und fuehrt zu keinem Reconnect-Dauerlauf."""
    source = make_source(server, password="falsch")

    with pytest.raises(ImapAuthError):
        await source.async_setup()

    await source.async_start()
    await wait_for(lambda: source.state is ImapState.ERROR)
    assert server.failed_logins >= 1

    # Der Loop hat sich beendet, statt gegen den Server zu laufen.
    attempts = server.login_attempts
    await asyncio.sleep(0.2)
    assert server.login_attempts == attempts
    with pytest.raises(ImapAuthError):
        await source.async_fetch()


async def test_missing_folder_is_a_permanent_error(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Ein nicht vorhandener Ordner ist ein Konfigurationsfehler, kein Verbindungsproblem."""
    source = make_source(server, folder="Gibt-Es-Nicht")

    with pytest.raises(ImapFolderError):
        await source.async_setup()


async def test_unreachable_server_reports_connection_error(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Ein toter Server ist ein Verbindungsfehler — kein dauerhafter Fehler."""
    source = make_source(server, port=server.port, command_timeout=0.5)
    await server.stop()

    with pytest.raises(ImapConnectionError):
        await source.async_setup()
    assert source.state is not ImapState.ERROR


async def test_first_run_honours_lookback(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Der Erststart schaut nur die konfigurierten Tage zurueck."""
    now = dt_util.utcnow()
    server.add_message(build_message(subject="Uralt"), date=now - timedelta(days=30))
    server.add_message(build_message(subject="Von gestern"), date=now - timedelta(days=1))
    handler = RecordingHandler()
    source = make_source(server, lookback_days=7, handler=handler)

    await source.async_setup()
    await source.async_fetch()

    assert handler.uids == [2]


async def test_rescan_reads_seen_mails_of_lookback_in_blocks(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Neu-Einlesen holt auch gelesene Mails im Zeitraum, blockweise, und laeuft dann normal."""
    now = dt_util.utcnow()
    server.add_message(build_message(subject="Uralt"), date=now - timedelta(days=30))
    for index in range(3):
        server.add_message(build_message(subject=f"Gelesen {index}"), flags=["\\Seen"])
    handler = RecordingHandler()
    source = make_source(server, lookback_days=7, max_messages=2, handler=handler)
    await source.async_setup()
    await source.async_fetch()
    assert handler.uids == []
    last_uid = source.progress.last_uid

    await source.async_rescan()
    assert source.rescanning
    await source.async_fetch()
    await source.async_fetch()

    assert handler.uids == [2, 3, 4]
    assert not source.rescanning
    assert source.progress.last_uid >= last_uid

    # Danach wieder die normale Suche: nur Neues.
    server.add_message(build_message(subject="Neu"))
    await source.async_fetch()
    assert handler.uids == [2, 3, 4, 5]


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        ("UNSEEN", []),
        ("NOT SEEN", []),
        ("UNSEEN FROM dhl.de", ["ALL", "FROM", "dhl.de"]),
        ("NOT SEEN FROM dhl.de", ["ALL", "FROM", "dhl.de"]),
        ("OR UNSEEN FLAGGED", ["OR", "ALL", "FLAGGED"]),
        ("OR NOT SEEN FLAGGED", ["OR", "ALL", "FLAGGED"]),
        ("FROM shop.de", ["FROM", "shop.de"]),
    ],
)
def test_rescan_criteria_drop_read_state_without_breaking_syntax(
    search: str, expected: list[str]
) -> None:
    """Kein `NOT` und kein `OR` ohne Argument bleibt zurueck."""
    assert _without_read_state(search.split()) == expected


async def test_rescan_ends_when_server_rejects_search(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Lehnt der Server die Suche ab, endet das Neu-Einlesen; die normale Suche laeuft weiter."""
    handler = RecordingHandler()
    # Das Fake kennt `FROM` nicht und antwortet mit BAD — wie ein Server bei kaputter Syntax.
    source = make_source(server, search="UNSEEN FROM shop.de", handler=handler)
    await source.async_setup()

    await source.async_rescan()
    await source.async_fetch()

    assert not source.rescanning
    assert source.state is not ImapState.ERROR


async def test_large_mailbox_is_read_in_blocks(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Pro Durchlauf wird nur ein Block geholt; der Rest folgt im naechsten."""
    for index in range(5):
        server.add_message(build_message(subject=f"Paket {index}"))
    handler = RecordingHandler()
    source = make_source(server, max_messages=2, handler=handler)

    await source.async_setup()
    assert len(await source.async_fetch()) == 2
    assert len(await source.async_fetch()) == 2
    assert len(await source.async_fetch()) == 1
    assert await source.async_fetch() == []
    assert handler.uids == [1, 2, 3, 4, 5]


async def test_failing_handler_keeps_mail_unread(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Eine Mail, an der die Verarbeitung scheitert, bleibt ungelesen im Postfach."""
    for index in range(3):
        server.add_message(build_message(subject=f"Paket {index}"))
    handler = RecordingHandler(fail_for={2})
    source = make_source(server, handler=handler)

    await source.async_setup()
    shipments = await source.async_fetch()

    assert handler.uids == [1, 3]
    assert [shipment.tracking_number for shipment in shipments] == ["1", "3"]
    seen = {message.uid: message.seen for message in server.mailbox().messages}
    assert seen == {1: True, 2: False, 3: True}


async def test_credentials_never_appear_in_logs(
    server: FakeImapServer,
    make_source: Callable[..., ImapSource],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Zugangsdaten stehen in keinem Log — auch nicht auf Debug-Level."""
    caplog.set_level("DEBUG")
    server.add_message(build_message())
    source = make_source(server, handler=RecordingHandler())

    await source.async_setup()
    await source.async_fetch()
    await source.async_close()

    assert server.password not in caplog.text
    assert server.password not in repr(source.config)
    assert server.password not in str(source.diagnostics())


async def test_diagnostics_and_progress_survive_serialisation(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Der Fortschritt ist serialisierbar und die Diagnose zeigt den Betriebszustand."""
    server.add_message(build_message())
    source = make_source(server, handler=RecordingHandler())

    await source.async_setup()
    await source.async_fetch()

    restored = ImapProgress.from_dict(source.progress.to_dict())
    assert restored == source.progress

    diagnostics = source.diagnostics()
    assert diagnostics["state"] == ImapState.CONNECTED.value
    assert diagnostics["mode"] == ImapMode.PUSH.value
    assert diagnostics["last_uid"] == 1
    assert diagnostics["processed_messages"] == 1
    assert "username" not in diagnostics


async def test_stored_progress_skips_already_processed_uids(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Ein uebernommener Fortschritt verhindert den erneuten Abruf alter Mails."""
    first = server.add_message(build_message(subject="Schon erledigt"))
    handler = RecordingHandler()
    source = make_source(
        server,
        handler=handler,
        progress=ImapProgress(uid_validity=1000, last_uid=first.uid),
    )

    await source.async_setup()
    assert await source.async_fetch() == []

    server.add_message(build_message(subject="Neu"))
    assert len(await source.async_fetch()) == 1
    assert handler.uids == [2]


@pytest.mark.parametrize(
    ("security", "with_ssl", "with_starttls"),
    [(ImapSecurity.SSL, True, False), (ImapSecurity.STARTTLS, False, True)],
)
async def test_encrypted_transports(
    security: ImapSecurity,
    with_ssl: bool,
    with_starttls: bool,
    make_source: Callable[..., ImapSource],
) -> None:
    """SSL und STARTTLS funktionieren gegen einen echten TLS-Socket."""
    context = server_ssl_context()
    server = FakeImapServer(
        ssl_context=context if with_ssl else None,
        starttls_context=context if with_starttls else None,
    )
    await server.start()
    try:
        server.add_message(build_message())
        handler = RecordingHandler()
        source = make_source(server, security=security, verify_ssl=False, handler=handler)

        await source.async_setup()
        assert len(await source.async_fetch()) == 1
        assert handler.uids == [1]

        await source.async_close()
    finally:
        await server.stop()


async def test_use_idle_false_forces_polling(
    server: FakeImapServer, make_source: Callable[..., ImapSource]
) -> None:
    """Ist IDLE abgeschaltet, wird gepollt, obwohl der Server IDLE anbietet."""
    source = make_source(server, use_idle=False)

    await source.async_setup()
    await source.async_start()
    await asyncio.sleep(0.2)

    assert source.mode is ImapMode.POLLING
    assert server.idle_starts == 0


async def test_list_folders_returns_selectable_folders_without_selecting() -> None:
    """Die Ordnerliste: INBOX zuerst, ohne \\Noselect, mit lesbaren Umlauten."""
    server = FakeImapServer(
        folders=("Pakete", "INBOX", "Gel&APY-scht", "Sent Items"),
        noselect_folders=("Archiv",),
    )
    await server.start()
    try:
        source = ImapSource(make_config(server))
        folders = await source.async_list_folders()
    finally:
        await server.stop()

    assert [folder.name for folder in folders] == [
        "INBOX",
        "Gel&APY-scht",
        "Pakete",
        "Sent Items",
    ]
    assert folders[1].label == "Gelöscht"
    assert source.state is ImapState.DISCONNECTED


async def test_list_folders_rejects_wrong_password(server: FakeImapServer) -> None:
    """Falsche Zugangsdaten fallen schon beim Auflisten auf."""
    source = ImapSource(make_config(server, password="falsch"))

    with pytest.raises(ImapAuthError):
        await source.async_list_folders()


async def test_folder_with_space_can_be_selected(make_source: Callable[..., ImapSource]) -> None:
    """Ordnernamen mit Leerzeichen werden fuer SELECT in Anfuehrungszeichen gesetzt."""
    server = FakeImapServer(folders=("INBOX", "Sent Items"))
    await server.start()
    try:
        server.add_message(build_message(), folder="Sent Items")
        handler = RecordingHandler()
        source = make_source(server, folder="Sent Items", handler=handler)

        await source.async_setup()
        assert len(await source.async_fetch()) == 1
        await source.async_close()
    finally:
        await server.stop()


def test_config_from_mapping_uses_defaults() -> None:
    """Subentry-Daten ohne optionale Felder ergeben eine gueltige Konfiguration."""
    config = ImapConfig.from_mapping(
        {"server": "imap.example.org", "username": "max", "password": "geheim", "port": None}
    )

    assert config.resolved_port == 993
    assert config.security is ImapSecurity.SSL
    assert config.folder == "INBOX"
    assert config.use_idle is True
    assert "geheim" not in repr(config)
