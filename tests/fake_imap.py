"""Ein echter, kleiner IMAP4rev1-Server fuer die Tests.

Kein Mock: Der Server spricht ueber einen echten Socket und implementiert genau
die Befehle, die die Integration benutzt — LOGIN, LIST, SELECT, UID SEARCH, UID FETCH,
UID STORE, IDLE, NOOP, CLOSE, LOGOUT, CAPABILITY und optional STARTTLS.

Damit lassen sich die Faelle testen, die mit Mocks nur behauptet werden koennen:
Push per IDLE, ein Server ohne IDLE-Capability, ein mitten im Betrieb
abgerissener Socket und ein Ordner, dessen `UIDVALIDITY` sich geaendert hat.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import ssl
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from homeassistant.util import dt as dt_util

CRLF = b"\r\n"

DEFAULT_CAPABILITIES = ("IMAP4rev1", "IDLE")
"""Capabilities des Standardservers. Ohne `IDLE` faellt der Client auf Polling zurueck."""

MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# Tokenizer: entweder ein Ausdruck in Anfuehrungszeichen oder alles ohne Leerzeichen.
_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')


class BadCommand(Exception):
    """Ein Befehl, den dieser Server nicht versteht — beantwortet mit `BAD`."""


@dataclass(slots=True)
class FakeMessage:
    """Eine Mail im Fake-Postfach."""

    uid: int
    raw: bytes
    internal_date: datetime
    flags: set[str] = field(default_factory=set)

    @property
    def seen(self) -> bool:
        """True, wenn die Mail als gelesen markiert ist."""
        return "\\Seen" in self.flags

    def flag_string(self) -> str:
        """Flags in der Schreibweise der FETCH-Antwort."""
        return " ".join(sorted(self.flags))


class FakeMailbox:
    """Ein Ordner mit UID-Vergabe und IDLE-Benachrichtigung."""

    def __init__(self, name: str, *, uid_validity: int = 1000) -> None:
        """Leeren Ordner anlegen."""
        self.name = name
        self.uid_validity = uid_validity
        self.uid_next = 1
        self.messages: list[FakeMessage] = []
        self._subscribers: set[asyncio.Queue[bytes]] = set()

    def add(
        self,
        raw: bytes,
        *,
        date: datetime | None = None,
        flags: Iterable[str] = (),
    ) -> FakeMessage:
        """Eine Mail zustellen und alle IDLE-Verbindungen aufwecken."""
        message = FakeMessage(
            uid=self.uid_next,
            raw=raw,
            internal_date=date or dt_util.utcnow(),
            flags=set(flags),
        )
        self.uid_next += 1
        self.messages.append(message)
        self._notify()
        return message

    def reset(self, *, uid_validity: int) -> None:
        """Den Ordner neu aufsetzen — wie nach einem Serverumzug."""
        self.uid_validity = uid_validity
        self.uid_next = 1
        self.messages = []

    def sequence_number(self, message: FakeMessage) -> int:
        """Laufende Nummer der Mail im Ordner (1-basiert)."""
        return self.messages.index(message) + 1

    @property
    def unseen(self) -> list[FakeMessage]:
        """Alle ungelesenen Mails."""
        return [message for message in self.messages if not message.seen]

    def subscribe(self, queue: asyncio.Queue[bytes]) -> None:
        """Eine IDLE-Verbindung anmelden."""
        self._subscribers.add(queue)

    def unsubscribe(self, queue: asyncio.Queue[bytes]) -> None:
        """Eine IDLE-Verbindung abmelden."""
        self._subscribers.discard(queue)

    def _notify(self) -> None:
        """Allen wartenden Verbindungen den neuen Stand melden."""
        payload = (f"* {len(self.messages)} EXISTS\r\n* {len(self.unseen)} RECENT\r\n").encode()
        for queue in self._subscribers:
            queue.put_nowait(payload)


class FakeImapServer:
    """Der Testserver. `start()` bindet auf einen freien Port auf 127.0.0.1."""

    def __init__(
        self,
        *,
        username: str = "tester",
        password: str = "s3cr3t-pw",
        capabilities: Sequence[str] = DEFAULT_CAPABILITIES,
        folders: Sequence[str] = ("INBOX",),
        noselect_folders: Sequence[str] = (),
        ssl_context: ssl.SSLContext | None = None,
        starttls_context: ssl.SSLContext | None = None,
    ) -> None:
        """Server konfigurieren; gestartet wird er erst mit `start`.

        `noselect_folders` tauchen in LIST auf, lassen sich aber nicht oeffnen —
        wie reine Elternordner bei vielen Servern.
        """
        self.username = username
        self.password = password
        self.capabilities = list(capabilities)
        if starttls_context is not None and "STARTTLS" not in self.capabilities:
            self.capabilities.append("STARTTLS")
        self.mailboxes = {name: FakeMailbox(name) for name in folders}
        self.noselect_folders = list(noselect_folders)
        self.ssl_context = ssl_context
        self.starttls_context = starttls_context
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.port = 0

        # Zaehler fuer die Tests.
        self.login_attempts = 0
        self.failed_logins = 0
        self.idle_starts = 0
        self.fetched_uids: list[int] = []
        self.searches: list[str] = []

    # ------------------------------------------------------------------
    # Steuerung
    # ------------------------------------------------------------------

    async def start(self) -> int:
        """Server starten und den gebundenen Port zurueckgeben."""
        self._server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", 0, ssl=self.ssl_context
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        """Alle Verbindungen und den Listener schliessen."""
        self.drop_connections()
        if self._server is not None:
            self._server.close()
            # `wait_closed` wartet auch auf alle Verbindungen; ein Client, der
            # gerade neu verbindet, darf das Testende nicht blockieren.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            self._server = None

    def drop_connections(self) -> int:
        """Alle offenen Verbindungen hart abreissen — wie ein Serverneustart."""
        writers, self._writers = self._writers, set()
        for writer in writers:
            with contextlib.suppress(Exception):
                writer.transport.abort()
        return len(writers)

    @property
    def connections(self) -> int:
        """Anzahl offener Verbindungen."""
        return len(self._writers)

    def mailbox(self, name: str = "INBOX") -> FakeMailbox:
        """Einen Ordner holen."""
        return self.mailboxes[name]

    def add_message(
        self,
        raw: bytes,
        *,
        folder: str = "INBOX",
        date: datetime | None = None,
        flags: Iterable[str] = (),
    ) -> FakeMessage:
        """Eine Mail zustellen (weckt wartende IDLE-Verbindungen)."""
        return self.mailboxes[folder].add(raw, date=date, flags=flags)

    # ------------------------------------------------------------------
    # Verbindungen
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Eine Verbindung von Begruessung bis Verbindungsende bedienen."""
        self._writers.add(writer)
        session = _Session(self, reader, writer)
        try:
            await session.run()
        except ConnectionError, asyncio.IncompleteReadError, ssl.SSLError:
            pass  # Abgerissene Verbindungen sind in diesen Tests der Normalfall.
        finally:
            self._writers.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()


class _Session:
    """Der Zustand einer einzelnen Verbindung."""

    def __init__(
        self, server: FakeImapServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Sitzung im Zustand `nonauth` anlegen."""
        self._server = server
        self._reader = reader
        self._writer = writer
        self._authenticated = False
        self._selected: FakeMailbox | None = None

    async def run(self) -> None:
        """Befehle lesen und beantworten, bis die Gegenseite geht."""
        self._send(f"* OK [CAPABILITY {self._capabilities()}] FakeIMAP bereit")
        while True:
            line = await self._reader.readline()
            if not line:
                return
            tokens = _tokenize(line.decode("utf-8", "replace").rstrip("\r\n"))
            if not tokens:
                continue
            tag, command, args = tokens[0], tokens[1].upper() if len(tokens) > 1 else "", tokens[2:]
            try:
                if await self._dispatch(tag, command, args):
                    return
            except BadCommand as err:
                self._send(f"{tag} BAD {err}")

    async def _dispatch(self, tag: str, command: str, args: list[str]) -> bool:
        """Einen Befehl ausfuehren. True beendet die Verbindung."""
        match command:
            case "CAPABILITY":
                self._send(f"* CAPABILITY {self._capabilities()}")
                self._send(f"{tag} OK CAPABILITY completed")
            case "NOOP":
                self._send(f"{tag} OK NOOP completed")
            case "LOGOUT":
                self._send("* BYE FakeIMAP macht Schluss")
                self._send(f"{tag} OK LOGOUT completed")
                return True
            case "STARTTLS":
                await self._cmd_starttls(tag)
            case "LOGIN":
                self._cmd_login(tag, args)
            case "SELECT" | "EXAMINE":
                self._cmd_select(tag, args)
            case "LIST":
                self._cmd_list(tag)
            case "CLOSE":
                self._selected = None
                self._send(f"{tag} OK CLOSE completed")
            case "IDLE":
                await self._cmd_idle(tag)
            case "UID":
                self._cmd_uid(tag, args)
            case "SEARCH":
                self._cmd_search(tag, args, by_uid=False)
            case _:
                raise BadCommand(f"unbekannter Befehl {command}")
        return False

    # -- Befehle -------------------------------------------------------

    async def _cmd_starttls(self, tag: str) -> None:
        """Die Verbindung auf TLS heben."""
        context = self._server.starttls_context
        if context is None:
            raise BadCommand("STARTTLS nicht angeboten")
        self._send(f"{tag} OK STARTTLS bereit")
        await self._writer.drain()
        await self._writer.start_tls(context)

    def _cmd_login(self, tag: str, args: list[str]) -> None:
        """Anmeldung pruefen."""
        self._server.login_attempts += 1
        if len(args) < 2 or (args[0], args[1]) != (self._server.username, self._server.password):
            self._server.failed_logins += 1
            self._send(f"{tag} NO [AUTHENTICATIONFAILED] Anmeldung fehlgeschlagen")
            return
        self._authenticated = True
        self._send(f"{tag} OK [CAPABILITY {self._capabilities()}] LOGIN completed")

    def _cmd_select(self, tag: str, args: list[str]) -> None:
        """Ordner auswaehlen."""
        self._require_auth()
        name = args[0] if args else ""
        mailbox = self._server.mailboxes.get(name)
        if mailbox is None:
            self._send(f"{tag} NO [NONEXISTENT] Ordner {name} existiert nicht")
            return
        self._selected = mailbox
        self._send(r"* FLAGS (\Seen \Answered \Flagged \Deleted \Draft)")
        self._send(f"* {len(mailbox.messages)} EXISTS")
        self._send(f"* {len(mailbox.unseen)} RECENT")
        self._send(f"* OK [UIDVALIDITY {mailbox.uid_validity}] UIDs gueltig")
        self._send(f"* OK [UIDNEXT {mailbox.uid_next}] naechste UID")
        self._send(f"{tag} OK [READ-WRITE] SELECT completed")

    def _cmd_list(self, tag: str) -> None:
        """Alle Ordner auflisten (Referenz und Muster werden ignoriert)."""
        self._require_auth()
        for name in self._server.mailboxes:
            self._send(f'* LIST (\\HasNoChildren) "/" "{name}"')
        for name in self._server.noselect_folders:
            self._send(f'* LIST (\\Noselect \\HasChildren) "/" "{name}"')
        self._send(f"{tag} OK LIST completed")

    def _cmd_uid(self, tag: str, args: list[str]) -> None:
        """`UID SEARCH`, `UID FETCH` und `UID STORE`."""
        if not args:
            raise BadCommand("UID ohne Unterbefehl")
        match args[0].upper():
            case "SEARCH":
                self._cmd_search(tag, args[1:], by_uid=True)
            case "FETCH":
                self._cmd_fetch(tag, args[1:])
            case "STORE":
                self._cmd_store(tag, args[1:])
            case other:
                raise BadCommand(f"UID {other} wird nicht unterstuetzt")

    def _cmd_search(self, tag: str, args: list[str], *, by_uid: bool) -> None:
        """Suche nach den unterstuetzten Kriterien."""
        mailbox = self._require_selected()
        self._server.searches.append(" ".join(args))
        matches = _search(mailbox, args)
        ids = [
            str(message.uid if by_uid else mailbox.sequence_number(message)) for message in matches
        ]
        self._send(f"* SEARCH {' '.join(ids)}".rstrip())
        self._send(f"{tag} OK SEARCH completed")

    def _cmd_fetch(self, tag: str, args: list[str]) -> None:
        """Mails ausliefern; `BODY[]` markiert dabei als gelesen, `BODY.PEEK[]` nicht."""
        mailbox = self._require_selected()
        if len(args) < 2:
            raise BadCommand("FETCH ohne Angabe der Teile")
        parts = _parts(args[1:])
        for message in _messages_in_set(mailbox, args[0]):
            self._server.fetched_uids.append(message.uid)
            if "BODY[]" in parts:
                message.flags.add("\\Seen")
            self._send_fetch(mailbox, message, parts)
        self._send(f"{tag} OK FETCH completed")

    def _send_fetch(self, mailbox: FakeMailbox, message: FakeMessage, parts: set[str]) -> None:
        """Eine FETCH-Antwort mit dem Nachrichtentext als Literal schreiben."""
        header = f"* {mailbox.sequence_number(message)} FETCH (UID {message.uid}"
        header += f" FLAGS ({message.flag_string()})"
        body = parts & {"BODY[]", "BODY.PEEK[]", "RFC822"}
        if not body:
            self._write(f"{header})".encode() + CRLF)
            return
        name = "RFC822" if "RFC822" in body else "BODY[]"
        header += f" {name} {{{len(message.raw)}}}"
        # Literal und schliessende Klammer gehoeren an dieselbe Zeile wie bei echten Servern.
        self._write(header.encode() + CRLF + message.raw + b")" + CRLF)

    def _cmd_store(self, tag: str, args: list[str]) -> None:
        """Flags setzen, entfernen oder ersetzen."""
        mailbox = self._require_selected()
        if len(args) < 2:
            raise BadCommand("STORE ohne Flags")
        operation = args[1].upper()
        silent = operation.endswith(".SILENT")
        flags = _flags(args[2:])
        for message in _messages_in_set(mailbox, args[0]):
            if operation.startswith("+"):
                message.flags |= flags
            elif operation.startswith("-"):
                message.flags -= flags
            else:
                message.flags = set(flags)
            if not silent:
                self._send(
                    f"* {mailbox.sequence_number(message)} FETCH "
                    f"(UID {message.uid} FLAGS ({message.flag_string()}))"
                )
        self._send(f"{tag} OK STORE completed")

    async def _cmd_idle(self, tag: str) -> None:
        """IDLE, bis der Client `DONE` schickt oder eine Mail eintrifft."""
        mailbox = self._require_selected()
        if "IDLE" not in self._server.capabilities:
            raise BadCommand("IDLE wird nicht angeboten")
        self._server.idle_starts += 1
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        mailbox.subscribe(queue)
        self._send("+ idling")

        done = asyncio.ensure_future(self._reader.readline())
        push = asyncio.ensure_future(queue.get())
        try:
            while not done.done():
                await asyncio.wait({done, push}, return_when=asyncio.FIRST_COMPLETED)
                if push.done():
                    self._write(push.result())
                    push = asyncio.ensure_future(queue.get())
            closing_line = done.result()
        finally:
            mailbox.unsubscribe(queue)
            for task in (done, push):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        if not closing_line:
            raise ConnectionResetError("Verbindung waehrend IDLE beendet")
        self._send(f"{tag} OK IDLE terminated")

    # -- Hilfsmittel ---------------------------------------------------

    def _capabilities(self) -> str:
        """Capability-Liste als Text."""
        return " ".join(self._server.capabilities)

    def _require_auth(self) -> None:
        """Sicherstellen, dass die Sitzung angemeldet ist."""
        if not self._authenticated:
            raise BadCommand("Befehl ist ohne Anmeldung nicht erlaubt")

    def _require_selected(self) -> FakeMailbox:
        """Sicherstellen, dass ein Ordner ausgewaehlt ist."""
        self._require_auth()
        if self._selected is None:
            raise BadCommand("Befehl braucht einen ausgewaehlten Ordner")
        return self._selected

    def _send(self, line: str) -> None:
        """Eine Textzeile mit CRLF schicken."""
        self._write(line.encode() + CRLF)

    def _write(self, data: bytes) -> None:
        """Rohdaten schicken; auf einer toten Verbindung stillschweigend verwerfen."""
        with contextlib.suppress(Exception):
            self._writer.write(data)


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------


def _tokenize(line: str) -> list[str]:
    """Eine Befehlszeile in Tokens zerlegen; Anfuehrungszeichen fallen weg."""
    return [
        (quoted if quoted else bare).replace('\\"', '"').replace("\\\\", "\\")
        for quoted, bare in _TOKEN_RE.findall(line)
    ]


def _parts(tokens: Iterable[str]) -> set[str]:
    """Klammer-Listen wie `(UID BODY.PEEK[])` zu einer Menge aufloesen."""
    return {token.upper() for token in _flags(tokens)}


def _flags(tokens: Iterable[str]) -> set[str]:
    """Wie `_parts`, aber ohne die Schreibweise anzutasten — Flags bleiben `\\Seen`."""
    return {stripped for token in tokens if (stripped := token.strip("()"))}


def _messages_in_set(mailbox: FakeMailbox, uid_set: str) -> list[FakeMessage]:
    """Alle Mails, deren UID im angegebenen Set liegt."""
    return [message for message in mailbox.messages if _in_uid_set(message.uid, uid_set, mailbox)]


def _in_uid_set(uid: int, uid_set: str, mailbox: FakeMailbox) -> bool:
    """Ein UID-Set der Form `3`, `1,5`, `2:7` oder `5:*` auswerten."""
    highest = max((message.uid for message in mailbox.messages), default=0)
    for part in uid_set.split(","):
        start, separator, end = part.partition(":")
        if not separator:
            if start.isdigit() and int(start) == uid:
                return True
            continue
        low = highest if start == "*" else int(start)
        high = highest if end == "*" else int(end)
        # `5:*` schliesst laut RFC 3501 immer die hoechste UID ein, auch wenn sie
        # kleiner als 5 ist — genau daran haengt der UID-Filter im Client.
        if min(low, high) <= uid <= max(low, high):
            return True
    return False


def _search(mailbox: FakeMailbox, tokens: Sequence[str]) -> list[FakeMessage]:
    """Die unterstuetzten Suchkriterien nacheinander anwenden."""
    result = list(mailbox.messages)
    index = 0
    while index < len(tokens):
        token = tokens[index].upper()
        index += 1
        match token:
            case "CHARSET":
                index += 1
            case "ALL":
                pass
            case "UNSEEN":
                result = [message for message in result if not message.seen]
            case "SEEN":
                result = [message for message in result if message.seen]
            case "UID":
                uid_set = tokens[index]
                index += 1
                result = [
                    message for message in result if _in_uid_set(message.uid, uid_set, mailbox)
                ]
            case "SINCE":
                limit = _parse_imap_date(tokens[index])
                index += 1
                result = [m for m in result if m.internal_date.date() >= limit]
            case "BEFORE":
                limit = _parse_imap_date(tokens[index])
                index += 1
                result = [m for m in result if m.internal_date.date() < limit]
            case _:
                raise BadCommand(f"Suchkriterium {token} wird nicht unterstuetzt")
    return result


def _parse_imap_date(value: str) -> date:
    """Ein Datum im IMAP-Format `01-Jan-2026` lesen."""
    day, month, year = value.strip('"').split("-")
    return date(int(year), MONTHS.index(month) + 1, int(day))


def imap_date(value: datetime) -> str:
    """Ein Datum im IMAP-Format schreiben (Gegenstueck fuer die Tests)."""
    return f"{value.day:02d}-{MONTHS[value.month - 1]}-{value.year}"


def build_message(
    *,
    subject: str = "Ihre Sendung ist unterwegs",
    sender: str = "noreply@example.org",
    message_id: str = "<1@example.org>",
    date: datetime | None = None,
    body: str = "Sendungsnummer 00340434161094042557",
) -> bytes:
    """Eine simple RFC822-Mail als Rohbytes."""
    when = date or dt_util.utcnow()
    return (
        f"From: {sender}\r\n"
        f"To: kunde@example.org\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Date: {when.strftime('%a, %d %b %Y %H:%M:%S +0000')}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode()


# ----------------------------------------------------------------------
# TLS fuer die Tests
# ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def _certificate() -> tuple[Path, Path]:
    """Ein selbstsigniertes Zertifikat fuer 127.0.0.1 erzeugen (einmal je Testlauf)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(tz=dt_util.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )

    directory = Path(tempfile.mkdtemp(prefix="fake-imap-tls-"))
    cert_file = directory / "cert.pem"
    key_file = directory / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


def server_ssl_context() -> ssl.SSLContext:
    """TLS-Kontext fuer den Fake-Server."""
    cert_file, key_file = _certificate()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context
