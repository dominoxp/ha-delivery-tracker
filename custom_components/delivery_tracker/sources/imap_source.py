"""IMAP-Quelle: liefert neue Mails per IDLE-Push, sonst per Polling.

Die Quelle holt Mails als Rohbytes und uebergibt sie einem `MailHandler`; das
Zerlegen und Erkennen passiert erst in Step 1.2. Damit bleibt hier genau ein
Thema uebrig: eine Verbindung, die auch dann zuverlaessig alle Mails genau
einmal liefert, wenn der Server sie zwischendurch trennt.

Festgelegtes Verhalten (Step 2.1 und 2.6 bauen darauf auf):

* **Falsches Passwort / fehlender Ordner** sind dauerhafte Fehler: Der
  Hintergrund-Loop endet, `state` wird `error`, und jeder weitere `async_fetch`
  wirft denselben Fehler. Kein Reconnect-Dauerlauf gegen einen Server, der
  ohnehin ablehnt (Gmail und Outlook sperren sonst das Konto).
* **Verbindungsverlust** ist dagegen Normalbetrieb: Reconnect mit exponentiellem
  Backoff bis `IMAP_BACKOFF_MAX`, ohne Fehlerzustand nach aussen.
* **Fortschritt** haelt zwei Marker: `\\Seen` auf dem Server (ueberlebt jeden
  Neustart) und die hoechste verarbeitete UID (spart die Suche ueber das ganze
  Postfach). Aendert sich `UIDVALIDITY`, sind alle UIDs wertlos und der
  Fortschritt wird zurueckgesetzt.
* **`\\Seen` erst nach erfolgreicher Verarbeitung** — sonst geht bei einem
  Absturz mitten in der Verarbeitung eine Sendung verloren.

Zugangsdaten stehen in keinem Log und in keinem `repr`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
import ssl
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Self

from aioimaplib import (
    AUTH,
    IMAP4,
    IMAP4_SSL,
    NONAUTH,
    SELECTED,
    AioImapException,
    Command,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.ssl import client_context, create_no_verify_ssl_context

from ..const import (
    CONF_FOLDER,
    CONF_LOOKBACK_DAYS,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_SECURITY,
    CONF_SERVER,
    CONF_USE_IDLE,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_IMAP_COMMAND_TIMEOUT,
    DEFAULT_IMAP_FOLDER,
    DEFAULT_IMAP_LOOKBACK_DAYS,
    DEFAULT_IMAP_MAX_MESSAGES,
    DEFAULT_IMAP_POLL_INTERVAL,
    DEFAULT_IMAP_PORT_PLAIN,
    DEFAULT_IMAP_PORT_SSL,
    DEFAULT_IMAP_SEARCH,
    IMAP_BACKOFF_FACTOR,
    IMAP_BACKOFF_MAX,
    IMAP_BACKOFF_START,
    IMAP_IDLE_REFRESH,
    SOURCE_IMAP,
)
from ..models import Shipment
from .base import SourceAuthError, SourceError, SourcePermanentError, TrackingSource

_LOGGER = logging.getLogger(__name__)

# `* OK [UIDVALIDITY 1234] ...` aus der SELECT-Antwort.
_UID_VALIDITY_RE: Final = re.compile(rb"UIDVALIDITY\s+(\d+)", re.IGNORECASE)

# Monatsnamen fuer das IMAP-Datumsformat (`SINCE 01-Jan-2026`) — bewusst ohne
# `strftime`, das je nach Locale uebersetzte Namen liefert.
_IMAP_MONTHS: Final = (
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

# Suchkriterien zum Lesestatus; das Neu-Einlesen laesst sie weg.
_READ_STATE_CRITERIA: Final = frozenset({"UNSEEN", "SEEN", "NEW", "OLD", "RECENT"})

# Fehler, die ein Verbindungsproblem beschreiben und einen Reconnect ausloesen
# (`ssl.SSLError` ist ein `OSError` und damit bereits enthalten).
_CONNECTION_ERRORS: Final = (AioImapException, TimeoutError, OSError)


class ImapSecurity(StrEnum):
    """Transportsicherheit der Verbindung."""

    SSL = "ssl"
    """Implizites TLS ab dem ersten Byte (Port 993) — der Normalfall."""
    STARTTLS = "starttls"
    """Klartext-Verbindung, die per STARTTLS auf TLS gehoben wird (Port 143)."""
    NONE = "none"
    """Unverschluesselt — nur fuer lokale Server und Tests."""


class ImapState(StrEnum):
    """Verbindungsstatus fuer den Diagnose-Sensor aus Step 2.3."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    """Dauerhafter Fehler (Anmeldung oder Ordner) — hier hilft nur Konfiguration."""


class ImapMode(StrEnum):
    """Wie neue Mails entdeckt werden."""

    UNKNOWN = "unknown"
    PUSH = "push"
    POLLING = "polling"


class ImapError(SourceError):
    """Basisfehler der IMAP-Quelle."""


class ImapConnectionError(ImapError):
    """Server nicht erreichbar oder Verbindung abgebrochen — wird wiederholt."""


class ImapPermanentError(ImapError, SourcePermanentError):
    """Fehler, den nur eine Konfigurationsaenderung behebt."""


class ImapAuthError(ImapPermanentError, SourceAuthError):
    """Benutzername oder Passwort werden abgelehnt."""


class ImapFolderError(ImapPermanentError):
    """Der konfigurierte Ordner existiert nicht oder ist nicht auswaehlbar."""


@dataclass(frozen=True, slots=True)
class RawMail:
    """Eine unveraenderte Mail, so wie sie vom Server kam.

    Das Parsen uebernimmt Step 1.2; hier interessiert nur, welche Mail aus
    welchem Ordner sie ist.
    """

    uid: int
    raw: bytes
    folder: str = DEFAULT_IMAP_FOLDER


MailHandler = Callable[[RawMail], list[Shipment] | Awaitable[list[Shipment]]]
"""Verarbeitet eine Rohmail zu Sendungs-Fragmenten (ab Step 1.2 die Mail-Pipeline)."""

TaskFactory = Callable[[Coroutine[Any, Any, None], str], "asyncio.Task[None]"]
"""Erzeugt den Hintergrund-Task; in HA `hass.async_create_background_task`."""


@dataclass(frozen=True, slots=True)
class ImapConfig:
    """Zugangsdaten und Verhalten einer Postfach-Verbindung."""

    server: str
    username: str
    password: str = field(repr=False)
    """Steht bewusst nicht im `repr` — Zugangsdaten gehoeren in kein Log."""
    port: int | None = None
    """`None` waehlt den Standardport passend zur Transportsicherheit."""
    security: ImapSecurity = ImapSecurity.SSL
    verify_ssl: bool = True
    folder: str = DEFAULT_IMAP_FOLDER
    search: str = DEFAULT_IMAP_SEARCH
    use_idle: bool = True
    """False erzwingt Polling, auch wenn der Server IDLE anbietet."""
    poll_interval: float = DEFAULT_IMAP_POLL_INTERVAL
    lookback_days: int = DEFAULT_IMAP_LOOKBACK_DAYS
    """Wie weit der allererste Durchlauf zurueckschaut; danach nur noch neue Mails."""
    max_messages: int = DEFAULT_IMAP_MAX_MESSAGES
    command_timeout: float = DEFAULT_IMAP_COMMAND_TIMEOUT
    backoff_start: float = IMAP_BACKOFF_START
    backoff_max: float = IMAP_BACKOFF_MAX

    @property
    def resolved_port(self) -> int:
        """Der tatsaechlich verwendete Port."""
        if self.port is not None:
            return self.port
        if self.security is ImapSecurity.SSL:
            return DEFAULT_IMAP_PORT_SSL
        return DEFAULT_IMAP_PORT_PLAIN

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        """Aus den Daten eines `mailbox`-Subentrys bauen."""
        port = data.get(CONF_PORT)
        return cls(
            server=str(data[CONF_SERVER]),
            username=str(data[CONF_USERNAME]),
            password=str(data[CONF_PASSWORD]),
            port=int(port) if port else None,
            security=ImapSecurity(data.get(CONF_SECURITY, ImapSecurity.SSL)),
            verify_ssl=bool(data.get(CONF_VERIFY_SSL, True)),
            folder=str(data.get(CONF_FOLDER) or DEFAULT_IMAP_FOLDER),
            use_idle=bool(data.get(CONF_USE_IDLE, True)),
            poll_interval=float(data.get(CONF_POLL_INTERVAL, DEFAULT_IMAP_POLL_INTERVAL)),
            lookback_days=int(data.get(CONF_LOOKBACK_DAYS, DEFAULT_IMAP_LOOKBACK_DAYS)),
        )


@dataclass(slots=True)
class ImapProgress:
    """Wie weit das Postfach abgearbeitet ist.

    Serialisierbar, damit der Fortschritt ab Step 2.2 einen Neustart ueberlebt.
    """

    uid_validity: int | None = None
    last_uid: int = 0

    def reset(self, uid_validity: int | None) -> None:
        """Von vorn beginnen, weil die UIDs des Ordners neu vergeben wurden."""
        self.uid_validity = uid_validity
        self.last_uid = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialisieren."""
        return {"uid_validity": self.uid_validity, "last_uid": self.last_uid}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Deserialisieren; unbrauchbare Werte bedeuten „von vorn"."""
        uid_validity = data.get("uid_validity")
        last_uid = data.get("last_uid", 0)
        return cls(
            uid_validity=int(uid_validity) if uid_validity is not None else None,
            last_uid=int(last_uid) if last_uid else 0,
        )


class ImapSource(TrackingSource):
    """Postfach-Quelle mit IDLE-Push und Polling-Fallback.

    Zwei Betriebsarten, die sich nicht ausschliessen:

    * `async_start()` startet den Hintergrund-Loop, der von sich aus liest —
      per IDLE oder im Polling-Intervall — und ueber `on_new_data` Bescheid gibt.
      Derselbe Callback meldet auch, wenn der Loop an einem dauerhaften Fehler
      endet; der naechste `async_fetch` wirft diesen dann.
    * `async_fetch()` liefert alles, was seit dem letzten Aufruf zusammenkam.
      Laeuft kein Hintergrund-Loop, holt der Aufruf die Mails selbst ab.
    """

    def __init__(
        self,
        config: ImapConfig,
        *,
        handler: MailHandler | None = None,
        progress: ImapProgress | None = None,
        on_new_data: Callable[[], None] | None = None,
        task_factory: TaskFactory | None = None,
        source_id: str = SOURCE_IMAP,
        priority: int | None = None,
    ) -> None:
        """Quelle anlegen; verbunden wird erst in `async_setup`."""
        super().__init__(source_id, priority=priority)
        self.config = config
        self.progress = progress or ImapProgress()
        self._handler = handler
        self._on_new_data = on_new_data
        self._task_factory = task_factory
        self._client: IMAP4 | None = None
        self._connection_lost = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._buffer: list[Shipment] = []
        self._supports_idle: bool | None = None
        self._more_pending = False
        self._permanent_error: ImapPermanentError | None = None
        self._state = ImapState.DISCONNECTED
        self._last_error: str | None = None
        self._last_success: datetime | None = None
        self._processed = 0
        self._rescan_after: int | None = None
        """Waehrend eines Neu-Einlesens: hoechste schon erneut gelesene UID."""

    # ------------------------------------------------------------------
    # Zustand nach aussen
    # ------------------------------------------------------------------

    @property
    def state(self) -> ImapState:
        """Aktueller Verbindungsstatus."""
        return self._state

    @property
    def mode(self) -> ImapMode:
        """Ob der Server Push kann — steht erst nach dem Verbinden fest."""
        if self._supports_idle is None:
            return ImapMode.UNKNOWN
        return ImapMode.PUSH if self._supports_idle else ImapMode.POLLING

    @property
    def available(self) -> bool:
        """True, solange die Verbindung nutzbar ist."""
        return self._state is ImapState.CONNECTED

    def diagnostics(self) -> dict[str, Any]:
        """Zustand fuer Step 2.6 — bewusst ohne Benutzername und Passwort."""
        return {
            "server": self.config.server,
            "port": self.config.resolved_port,
            "security": self.config.security.value,
            "folder": self.config.folder,
            "state": self._state.value,
            "mode": self.mode.value,
            "uid_validity": self.progress.uid_validity,
            "last_uid": self.progress.last_uid,
            "processed_messages": self._processed,
            "pending_shipments": len(self._buffer),
            "last_success": self._last_success.isoformat() if self._last_success else None,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------------
    # TrackingSource
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Verbinden, anmelden und den Ordner waehlen.

        Wirft `ImapAuthError`/`ImapFolderError` bei falscher Konfiguration und
        `ImapConnectionError`, wenn der Server gerade nicht erreichbar ist.
        """
        await self._async_ensure_connected()

    async def async_fetch(self) -> list[Shipment]:
        """Alle Sendungs-Fragmente seit dem letzten Aufruf."""
        if self._permanent_error is not None:
            raise self._permanent_error
        if self._runner is None or self._runner.done():
            await self._async_ensure_connected()
            await self._async_process_new_mail()
        buffered = self._buffer
        self._buffer = []
        return buffered

    async def async_rescan(self) -> None:
        """Alle Mails im Lookback-Zeitraum erneut verarbeiten, auch schon gelesene.

        Fuer die Fehlersuche in der Sendungsverwaltung: Das Mail-Protokoll
        beginnt sonst erst mit der naechsten neuen Mail. Gelesene Mails findet
        die normale Suche (`UNSEEN`) nicht mehr, deshalb laeuft das Neu-Einlesen
        mit eigenen Kriterien, blockweise wie gewohnt, und kehrt danach zur
        normalen Suche zurueck. Der Fortschritt bleibt dabei unangetastet.

        Ein laufender Hintergrund-Loop wird dafuer neu gestartet.
        """
        restart = self._runner is not None and not self._runner.done()
        if restart:
            await self.async_close()
        self._rescan_after = 0
        if restart:
            await self.async_start()

    @property
    def rescanning(self) -> bool:
        """True, solange ein Neu-Einlesen laeuft."""
        return self._rescan_after is not None

    async def async_close(self) -> None:
        """Loop beenden und Verbindung abbauen. Mehrfach aufrufbar."""
        runner, self._runner = self._runner, None
        if runner is not None and not runner.done():
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        await self._async_disconnect()

    # ------------------------------------------------------------------
    # Hintergrund-Loop
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Den Hintergrund-Loop starten (Push bzw. Polling inklusive Reconnect)."""
        if self._runner is not None and not self._runner.done():
            return
        name = f"delivery_tracker imap {self.config.server}"
        coro = self._async_run()
        self._runner = (
            self._task_factory(coro, name)
            if self._task_factory is not None
            else asyncio.create_task(coro, name=name)
        )

    async def _async_run(self) -> None:
        """Dauerschleife: lesen, warten, bei Verbindungsverlust neu verbinden."""
        backoff = self.config.backoff_start
        while True:
            try:
                await self._async_ensure_connected()
                await self._async_process_new_mail()
                backoff = self.config.backoff_start
                await self._async_wait_for_next_round()
            except ImapPermanentError as err:
                # Weiterprobieren waere sinnlos und bringt bei Gmail/Outlook eine Sperre ein.
                _LOGGER.error("%s: %s — die Quelle bleibt bis zur Korrektur stehen", self, err)
                await self._async_disconnect(ImapState.ERROR, err)
                # Der Abnehmer soll den Fehler sofort sehen, nicht erst beim naechsten Takt:
                # Der folgende `async_fetch` wirft ihn.
                if self._on_new_data is not None:
                    self._on_new_data()
                return
            except (ImapConnectionError, *_CONNECTION_ERRORS) as err:
                _LOGGER.debug(
                    "%s: Verbindung verloren (%s) — neuer Versuch in %.0f s", self, err, backoff
                )
                await self._async_disconnect(ImapState.DISCONNECTED, err)
                await asyncio.sleep(backoff)
                backoff = min(backoff * IMAP_BACKOFF_FACTOR, self.config.backoff_max)

    async def _async_wait_for_next_round(self) -> None:
        """Bis zum naechsten Durchlauf warten — per IDLE oder Intervall."""
        if self._more_pending:
            # Der Block war voll: sofort weiterlesen statt zu warten.
            return
        if self._supports_idle:
            await self._async_wait_for_push()
        else:
            await asyncio.sleep(self.config.poll_interval)

    async def _async_wait_for_push(self) -> None:
        """Im IDLE warten, bis der Server etwas meldet.

        `idle_start` beendet das IDLE nach `IMAP_IDLE_REFRESH` von selbst, damit
        der Server die Verbindung nicht wegen Untaetigkeit kappt. Sowohl das
        Aufsetzen des IDLE als auch das Warten darin brauchen die Absicherung
        gegen den Verbindungsverlust: Beide warten sonst auf eine Antwort, die
        von einem toten Socket nie kommt.
        """
        client = self._require_client()
        idle = await self._async_race_connection_loss(client.idle_start(timeout=IMAP_IDLE_REFRESH))
        try:
            await self._async_race_connection_loss(client.wait_server_push())
        finally:
            if not self._connection_lost.is_set():
                client.idle_done()
                with contextlib.suppress(*_CONNECTION_ERRORS):
                    await asyncio.wait_for(idle, self.config.command_timeout)
            if not idle.done():
                idle.cancel()
                with contextlib.suppress(asyncio.CancelledError, *_CONNECTION_ERRORS):
                    await idle

    async def _async_race_connection_loss[T](self, awaitable: Awaitable[T]) -> T:
        """Auf ein Ergebnis warten, aber nicht laenger als die Verbindung haelt."""
        target = asyncio.ensure_future(awaitable)
        lost = asyncio.ensure_future(self._connection_lost.wait())
        try:
            await asyncio.wait({target, lost}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (target, lost):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        if self._connection_lost.is_set():
            raise ImapConnectionError("Verbindung wurde vom Server getrennt")
        return target.result()

    # ------------------------------------------------------------------
    # Ein Durchlauf
    # ------------------------------------------------------------------

    async def _async_process_new_mail(self) -> None:
        """Neue Mails suchen, abholen, verarbeiten und als gelesen markieren."""
        client = self._require_client()
        uids = await self._async_search(client)
        self._more_pending = len(uids) > self.config.max_messages
        found = False

        try:
            for uid in uids[: self.config.max_messages]:
                raw = await self._async_fetch_message(client, uid)
                if raw is None:
                    _LOGGER.debug("%s: UID %d lieferte keinen Inhalt", self, uid)
                elif shipments := await self._async_handle(client, uid, raw):
                    # Sofort puffern, nicht erst am Ende des Blocks: Bricht die
                    # Verbindung mittendrin ab, waere sonst eine bereits mit
                    # `\Seen` markierte Mail verloren.
                    self._buffer.extend(shipments)
                    found = True
                # Auch uebersprungene UIDs zaehlen als erledigt, sonst haengt der
                # Fortschritt fuer immer an einer einzigen kaputten Mail fest.
                self.progress.last_uid = max(self.progress.last_uid, uid)
                if self._rescan_after is not None:
                    self._rescan_after = uid
            if self._rescan_after is not None and not self._more_pending:
                _LOGGER.debug("%s: Neu-Einlesen abgeschlossen", self)
                self._rescan_after = None
                # Was waehrenddessen neu ankam, holt die normale Suche nach.
                self._more_pending = True
        finally:
            if found and self._on_new_data is not None:
                self._on_new_data()

        self._last_success = dt_util.utcnow()

    async def _async_handle(self, client: IMAP4, uid: int, raw: bytes) -> list[Shipment]:
        """Eine Rohmail an den Handler geben und danach `\\Seen` setzen."""
        if self._handler is None:
            return []
        try:
            result = self._handler(RawMail(uid=uid, raw=raw, folder=self.config.folder))
            shipments = await result if isinstance(result, Awaitable) else result
        except Exception:
            # Eine kaputte Mail darf den Loop nie stoppen. Ohne `\Seen` und ohne
            # Ergebnis bleibt sie im Postfach sichtbar und wird nach einem
            # Neustart erneut versucht.
            _LOGGER.exception("%s: UID %d konnte nicht verarbeitet werden", self, uid)
            return []
        await self._async_mark_seen(client, uid)
        self._processed += 1
        return shipments

    async def _async_search(self, client: IMAP4) -> list[int]:
        """UIDs der noch nicht verarbeiteten Mails, aufsteigend."""
        criteria = self._search_criteria()
        # `charset=None` laesst das `CHARSET`-Argument weg; die Kriterien sind rein ASCII
        # und manche Server lehnen CHARSET bei SEARCH ab.
        response = await client.uid_search(*criteria, charset=None)
        if response.result != "OK":
            raise ImapConnectionError(f"SEARCH {' '.join(criteria)} fehlgeschlagen")
        if not response.lines:
            return []
        uids = sorted({int(token) for token in response.lines[0].split() if token.isdigit()})
        if self._rescan_after is not None:
            return [uid for uid in uids if uid > self._rescan_after]
        # `n:*` liefert bei leerem Bereich immer die hoechste UID mit — die muss weg.
        return [uid for uid in uids if uid > self.progress.last_uid]

    def _search_criteria(self) -> tuple[str, ...]:
        """Suchkriterien fuer diesen Durchlauf."""
        criteria: list[str] = []
        if self._rescan_after is not None:
            # Neu-Einlesen: der ganze Lookback-Zeitraum, gelesen oder nicht.
            if self._rescan_after:
                criteria += ["UID", f"{self._rescan_after + 1}:*"]
            if self.config.lookback_days > 0:
                since = dt_util.utcnow() - timedelta(days=self.config.lookback_days)
                criteria += ["SINCE", _imap_date(since)]
            criteria += [
                token
                for token in self.config.search.split()
                if token.upper() not in _READ_STATE_CRITERIA
            ]
            return tuple(criteria) or ("ALL",)
        if self.progress.last_uid:
            criteria += ["UID", f"{self.progress.last_uid + 1}:*"]
        elif self.config.lookback_days > 0:
            since = dt_util.utcnow() - timedelta(days=self.config.lookback_days)
            criteria += ["SINCE", _imap_date(since)]
        criteria += self.config.search.split()
        return tuple(criteria)

    async def _async_fetch_message(self, client: IMAP4, uid: int) -> bytes | None:
        """Eine Mail als Rohbytes holen, ohne sie dabei als gelesen zu markieren."""
        response = await client.uid("FETCH", str(uid), "BODY.PEEK[]")
        if response.result != "OK":
            raise ImapConnectionError(f"FETCH von UID {uid} fehlgeschlagen")
        # Literale legt aioimaplib als `bytearray` ab, alle anderen Zeilen als `bytes`.
        for line in response.lines:
            if isinstance(line, bytearray):
                return bytes(line)
        return None

    async def _async_mark_seen(self, client: IMAP4, uid: int) -> None:
        """Eine erfolgreich verarbeitete Mail als gelesen markieren."""
        response = await client.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Seen)")
        if response.result != "OK":
            # Kein Grund abzubrechen: Der UID-Fortschritt verhindert die Dublette ebenfalls.
            _LOGGER.warning("%s: UID %d liess sich nicht als gelesen markieren", self, uid)

    # ------------------------------------------------------------------
    # Verbindung
    # ------------------------------------------------------------------

    async def _async_ensure_connected(self) -> None:
        """Eine nutzbare Verbindung sicherstellen."""
        if self._client is not None and not self._connection_lost.is_set():
            return
        await self._async_disconnect()
        await self._async_connect()

    async def _async_connect(self) -> None:
        """Verbindung aufbauen, anmelden und den Ordner waehlen."""
        self._state = ImapState.CONNECTING
        lost = asyncio.Event()
        self._connection_lost = lost
        client = self._create_client(lost)
        try:
            await self._async_login(client)
            await self._async_select(client)
        except ImapError:
            await _async_shutdown_client(client)
            raise
        except _CONNECTION_ERRORS as err:
            await _async_shutdown_client(client)
            raise ImapConnectionError(f"Verbindung zu {self.config.server} fehlgeschlagen") from err

        self._client = client
        self._supports_idle = self.config.use_idle and client.has_capability("IDLE")
        self._state = ImapState.CONNECTED
        self._last_error = None
        _LOGGER.debug(
            "%s: verbunden, Ordner %s, Modus %s", self, self.config.folder, self.mode.value
        )

    async def _async_login(self, client: IMAP4) -> None:
        """Begruessung abwarten, ggf. STARTTLS und anmelden."""
        await client.wait_hello_from_server()
        if self.config.security is ImapSecurity.STARTTLS:
            await self._async_starttls(client)
        if client.protocol.state == NONAUTH:
            await client.login(self.config.username, self.config.password)
        if client.protocol.state not in (AUTH, SELECTED):
            raise ImapAuthError(f"Anmeldung an {self.config.server} abgelehnt")

    async def async_list_folders(self) -> list[MailFolder]:
        """Anmelden und die auswaehlbaren Ordner auflisten, ohne einen zu oeffnen.

        Fuer den Verbindungstest im Config-Flow: Die Verbindung wird danach
        sofort wieder geschlossen und beruehrt den Zustand dieser Quelle nicht.
        Wirft `ImapAuthError` bzw. `ImapConnectionError` wie `async_setup`.
        """
        client = self._create_client(asyncio.Event())
        try:
            await self._async_login(client)
            response = await client.list('""', "*")
        except ImapError:
            raise
        except _CONNECTION_ERRORS as err:
            raise ImapConnectionError(f"Verbindung zu {self.config.server} fehlgeschlagen") from err
        finally:
            await _async_shutdown_client(client)
        if response.result != "OK":
            # Ohne Ordnerliste bleibt im Flow die freie Eingabe — kein Grund zum Abbruch.
            _LOGGER.debug("%s: LIST wurde abgelehnt: %s", self, response.result)
            return []
        return _parse_folder_list(response.lines)

    def _create_client(self, lost: asyncio.Event) -> IMAP4:
        """Den aioimaplib-Client passend zur Transportsicherheit anlegen."""
        host = self.config.server
        port = self.config.resolved_port
        timeout = self.config.command_timeout
        if self.config.security is ImapSecurity.SSL:
            client: IMAP4 = IMAP4_SSL(host, port, timeout=timeout, ssl_context=self._ssl_context())
        else:
            client = IMAP4(host, port, timeout=timeout)
        # `IMAP4_SSL` reicht keinen Callback durch; nachtraeglich setzen geht bei beiden.
        client.protocol.conn_lost_cb = lambda exc: self._on_connection_lost(lost, exc)
        return client

    def _ssl_context(self) -> ssl.SSLContext:
        """TLS-Kontext; beide Varianten sind in HA zwischengespeichert."""
        return client_context() if self.config.verify_ssl else create_no_verify_ssl_context()

    def _on_connection_lost(self, lost: asyncio.Event, exc: Exception | None) -> None:
        """Callback von aioimaplib, wenn der Transport wegbricht."""
        lost.set()
        if lost is not self._connection_lost:
            return  # Nachzuegler einer bereits ersetzten Verbindung.
        self._last_error = str(exc) if exc else "Verbindung getrennt"
        if self._state is ImapState.CONNECTED:
            self._state = ImapState.DISCONNECTED

    async def _async_starttls(self, client: IMAP4) -> None:
        """Die Klartext-Verbindung auf TLS heben (RFC 2595)."""
        protocol = client.protocol
        if "STARTTLS" not in protocol.capabilities:
            raise ImapConnectionError(f"{self.config.server} bietet kein STARTTLS an")
        response = await asyncio.wait_for(
            protocol.execute(Command("STARTTLS", protocol.new_tag(), loop=protocol.loop)),
            self.config.command_timeout,
        )
        if response.result != "OK":
            raise ImapConnectionError(f"STARTTLS wurde abgelehnt: {response.result}")
        protocol.transport = await asyncio.get_running_loop().start_tls(
            protocol.transport,
            protocol,
            self._ssl_context(),
            server_hostname=self.config.server,
        )
        # Nach STARTTLS sind die alten Capabilities ungueltig (RFC 2595, Abschnitt 3.1).
        await protocol.capability()

    async def _async_select(self, client: IMAP4) -> None:
        """Ordner waehlen und den Fortschritt gegen `UIDVALIDITY` pruefen."""
        response = await client.select(_quote_mailbox(self.config.folder))
        if response.result != "OK" or client.protocol.state != SELECTED:
            raise ImapFolderError(f"Ordner {self.config.folder!r} ist nicht auswaehlbar")

        uid_validity = _parse_uid_validity(response.lines)
        if uid_validity is None or uid_validity == self.progress.uid_validity:
            return
        if self.progress.uid_validity is not None:
            _LOGGER.info(
                "%s: UIDVALIDITY von %s auf %s gewechselt — Fortschritt wird zurueckgesetzt",
                self,
                self.progress.uid_validity,
                uid_validity,
            )
        self.progress.reset(uid_validity)

    async def _async_disconnect(
        self, state: ImapState = ImapState.DISCONNECTED, error: Exception | None = None
    ) -> None:
        """Verbindung abbauen und den Status setzen."""
        client, self._client = self._client, None
        if client is not None:
            await _async_shutdown_client(client)
        self._state = state
        if error is not None:
            self._last_error = str(error)
        if isinstance(error, ImapPermanentError):
            self._permanent_error = error

    def _require_client(self) -> IMAP4:
        """Die aktive Verbindung — oder ein Verbindungsfehler."""
        if self._client is None:
            raise ImapConnectionError("keine Verbindung zum Postfach")
        return self._client


async def _async_shutdown_client(client: IMAP4) -> None:
    """Eine Verbindung geordnet schliessen; Fehler dabei sind bedeutungslos."""
    transport = client.protocol.transport
    # Auf einem toten Transport liefe jeder Befehl in seinen Timeout — dann waere
    # der Reconnect nach einem Abriss um mehrere Sekunden verzoegert.
    if transport is not None and not transport.is_closing():
        with contextlib.suppress(*_CONNECTION_ERRORS):
            if client.has_pending_idle():
                client.idle_done()
                await client.stop_wait_server_push()
            if client.protocol.state == SELECTED:
                await client.close()
            if client.protocol.state in (AUTH, SELECTED):
                await client.logout()

    # Die IDLE-Erneuerung von aioimaplib ist ein `call_later` ueber 25 Minuten und
    # ueberlebt sonst die Verbindung, die sie erneuern sollte.
    idle_waiter = getattr(client, "_idle_waiter", None)
    if idle_waiter is not None:
        idle_waiter.cancel()

    # Hintergrund-Tasks von aioimaplib (IDLE-Kommando, Warten auf die IDLE-Bestaetigung,
    # Begruessung). Auf einer toten Verbindung warten sie sonst endlos auf eine Antwort.
    pending = [task for task in (*client.tasks, *client.protocol.tasks) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    # Den Verbindungs-Task von aioimaplib aufloesen, sonst meckert asyncio ueber
    # eine nie abgeholte Exception (etwa bei „connection refused").
    task = getattr(client, "_client_task", None)
    if isinstance(task, asyncio.Task):
        if not task.done():
            task.cancel()
        elif not task.cancelled():
            task.exception()

    if client.protocol.transport is not None:
        client.protocol.transport.close()


def _parse_uid_validity(lines: Iterable[Any]) -> int | None:
    """`UIDVALIDITY` aus den Zeilen der SELECT-Antwort lesen."""
    for line in lines:
        if not isinstance(line, bytes | bytearray):
            continue
        match = _UID_VALIDITY_RE.search(line)
        if match:
            return int(match.group(1))
    return None


@dataclass(frozen=True, slots=True)
class MailFolder:
    """Ein auswaehlbarer Ordner aus der LIST-Antwort."""

    name: str
    """Name, wie ihn der Server erwartet (modified UTF-7, RFC 3501 Abschnitt 5.1.3)."""
    label: str
    """Lesbarer Name fuer die Auswahlliste."""


# `* LIST (\HasNoChildren) "/" "INBOX"` — aioimaplib liefert die Zeile ohne `* LIST`.
_LIST_LINE_RE: Final = re.compile(
    rb'^\((?P<flags>[^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) (?P<name>\S.*)$', re.IGNORECASE
)
_UNSELECTABLE_FLAGS: Final = (b"\\noselect", b"\\nonexistent")
_MUTF7_RE: Final = re.compile(r"&([A-Za-z0-9+,]*)-")
# Ordnernamen, die ohne Anfuehrungszeichen als IMAP-Atom durchgehen.
_ATOM_RE: Final = re.compile(r"^[A-Za-z0-9._/&+\-]+$")


def _parse_folder_list(lines: Iterable[Any]) -> list[MailFolder]:
    """Die auswaehlbaren Ordner aus den Zeilen einer LIST-Antwort lesen."""
    folders: dict[str, MailFolder] = {}
    for line in lines:
        # Ordnernamen als Literal (`{12}`) liefert kaum ein Server; sie fallen weg.
        if not isinstance(line, bytes):
            continue
        match = _LIST_LINE_RE.match(line)
        if match is None:
            continue
        if any(flag in match.group("flags").lower() for flag in _UNSELECTABLE_FLAGS):
            continue
        name = match.group("name").decode("utf-8", "replace").strip()
        if len(name) >= 2 and name[0] == name[-1] == '"':
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        folders.setdefault(name, MailFolder(name, _decode_modified_utf7(name)))
    # INBOX zuerst, der Rest alphabetisch — so steht der Normalfall oben.
    return sorted(
        folders.values(), key=lambda folder: (folder.name.upper() != "INBOX", folder.label)
    )


def _decode_modified_utf7(value: str) -> str:
    """Ordnernamen wie `Gel&APY-scht` lesbar machen; Unlesbares bleibt, wie es ist."""

    def _replace(match: re.Match[str]) -> str:
        encoded = match.group(1)
        if not encoded:
            return "&"
        encoded = encoded.replace(",", "/")
        return base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-16-be")

    try:
        return _MUTF7_RE.sub(_replace, value)
    except ValueError:
        return value


def _quote_mailbox(name: str) -> str:
    """Einen Ordnernamen fuer SELECT aufbereiten; „Sent Items" braucht Anfuehrungszeichen."""
    if _ATOM_RE.match(name) or (len(name) >= 2 and name[0] == name[-1] == '"'):
        return name
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _imap_date(value: datetime) -> str:
    """Ein Datum im IMAP-Format `01-Jan-2026`."""
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year}"
