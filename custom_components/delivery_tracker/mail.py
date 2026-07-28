"""Vorverarbeitung roher MIME-Nachrichten.

Carrier-Plugins sollen sich mit Zustellern beschaeftigen, nicht mit MIME. Deshalb
endet hier jede Eigenheit echter Mails: mehrteilige Nachrichten, `base64` und
`quoted-printable`, kaputte oder fehlende Zeichensatzangaben, HTML statt Text.

Zwei Regeln gelten ausnahmslos:

* **Nichts wirft.** Eine unlesbare Mail liefert notfalls Text mit Ersatzzeichen,
  aber nie eine Exception — sonst blockiert eine einzige kaputte Mail das Postfach.
* **Links bleiben erhalten.** Bei vielen Zustellern steht die Trackingnummer nur
  im Ziel des „Sendung verfolgen"-Buttons; HTML wird deshalb als `Text (URL)`
  konvertiert statt Tags einfach zu entfernen.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Final

from .models import ensure_aware

# Obergrenze fuer Body und HTML-Quelle. Werbemails mit eingebetteten Bildern
# erreichen mehrere Megabyte; die Trackingnummer steht nie dahinter.
MAX_BODY_LENGTH: Final = 200_000

# Ab welcher Laenge ein Text-Teil als brauchbar gilt. Darunter ist es meist der
# Platzhalter „Diese Nachricht benoetigt einen HTML-faehigen Client".
MIN_USEFUL_TEXT: Final = 40

# Kopfzeilen, die einen Empfaenger tragen koennen. `Delivered-To` und
# `X-Original-To` sind bei Weiterleitungen die einzige verlaessliche Quelle —
# Step 1.5 ordnet darueber Zieladressen zu.
_RECIPIENT_HEADERS: Final[tuple[str, ...]] = (
    "To",
    "Cc",
    "Delivered-To",
    "X-Original-To",
    "X-Envelope-To",
)

# Falsch geschriebene oder erfundene Zeichensatznamen sind in Mails Alltag.
# Ein leerer Wert bedeutet „Angabe ist wertlos, direkt raten".
_CHARSET_ALIASES: Final[dict[str, str]] = {
    "utf8": "utf-8",
    "utf-8n": "utf-8",
    "utf-08": "utf-8",
    "cp-1252": "cp1252",
    "windows-1252": "cp1252",
    "win-1252": "cp1252",
    "iso8859-1": "iso-8859-1",
    "iso-8859-1-windows-3.1-latin-1": "iso-8859-1",
    "latin1": "iso-8859-1",
    "latin-1": "iso-8859-1",
    "iso-8859-8-i": "iso-8859-8",
    "unknown-8bit": "",
    "unknown": "",
    "x-unknown": "",
    "default": "",
    "none": "",
    "0": "",
}

# Reihenfolge des Ratens: UTF-8 ist heute der Normalfall, cp1252 der haeufigste
# Windows-Fall, ISO-8859-1 kann jedes Byte und scheitert daher nie.
_FALLBACK_CHARSETS: Final[tuple[str, ...]] = ("utf-8", "cp1252", "iso-8859-1")

_CRLF: Final = re.compile(r"\r\n?")
_TRAILING_SPACE: Final = re.compile(r"[^\S\n]+\n")
_BLANK_LINES: Final = re.compile(r"\n{3,}")
_HORIZONTAL_SPACE: Final = re.compile(r"[^\S\n]+")


@dataclass(frozen=True, slots=True)
class MailMessage:
    """Eine Mail, so wie Carrier-Plugins sie zu sehen bekommen.

    Alles ist bereits dekodiert: Header ohne `=?UTF-8?B?…?=`, Body als lesbarer
    Text, HTML in Text ueberfuehrt. `html` bleibt zusaetzlich erhalten, damit
    Plugins mit Tabellen-Layouts (Amazon) daran arbeiten koennen.
    """

    message_id: str
    subject: str = ""
    text: str = ""
    """Body als reiner Text — HTML ist bereits konvertiert."""
    html: str | None = None
    """Original-HTML, falls vorhanden (fuer Plugins, die Links brauchen)."""
    from_address: str = ""
    from_name: str | None = None
    to_addresses: tuple[str, ...] = ()
    date: datetime | None = None
    list_unsubscribe: str | None = None
    """Inhalt von `List-Unsubscribe` — vorhanden heisst „Massenversand"."""
    uid: int | None = None
    """IMAP-UID, sofern die Mail aus einem Postfach stammt."""
    folder: str | None = None

    def __post_init__(self) -> None:
        """Zeitstempel normalisieren (das Objekt ist frozen, daher ueber `object`)."""
        if self.date is not None:
            object.__setattr__(self, "date", ensure_aware(self.date, field_name="MailMessage.date"))

    @property
    def from_domain(self) -> str:
        """Domain des Absenders in Kleinbuchstaben, leer wenn unbekannt."""
        _, separator, domain = self.from_address.rpartition("@")
        if not separator:
            return ""
        return domain.strip().strip(">").lower()

    @property
    def is_bulk(self) -> bool:
        """True bei Massenversand — Newsletter tragen fast immer `List-Unsubscribe`."""
        return bool(self.list_unsubscribe)

    @property
    def searchable_text(self) -> str:
        """Betreff und Body als ein Text — die Grundlage aller Regex-Regeln."""
        return f"{self.subject}\n{self.text}"


# ----------------------------------------------------------------------
# Zeichensaetze
# ----------------------------------------------------------------------


def _charset_candidates(charset: str | None) -> list[str]:
    """Zeichensaetze in der Reihenfolge, in der sie probiert werden."""
    candidates: list[str] = []
    if charset:
        name = _CHARSET_ALIASES.get(cleaned := charset.strip().strip("\"'").lower(), cleaned)
        if name:
            candidates.append(name)
    candidates += [name for name in _FALLBACK_CHARSETS if name not in candidates]
    return candidates


def decode_bytes(data: bytes, charset: str | None = None) -> str:
    """Bytes zu Text — notfalls mit Ersatzzeichen, aber nie mit Exception."""
    for candidate in _charset_candidates(charset):
        try:
            return data.decode(candidate)
        # LookupError: Zeichensatz gibt es nicht. ValueError deckt den
        # UnicodeDecodeError ab: die Bytes passen nicht zu diesem Zeichensatz.
        except LookupError, ValueError:
            continue
    return data.decode("utf-8", errors="replace")


def decode_header_value(value: object) -> str:
    """Eine Kopfzeile dekodieren (`=?UTF-8?Q?…?=`) und Faltung aufloesen."""
    if value is None:
        return ""
    raw = str(value)
    try:
        chunks = decode_header(raw)
    except Exception:  # kaputte Kodierung darf nie durchschlagen
        return _collapse(raw)

    parts: list[str] = []
    for data, charset in chunks:
        parts.append(decode_bytes(data, charset) if isinstance(data, bytes) else str(data))
    return _collapse("".join(parts))


def _collapse(value: str) -> str:
    """Whitespace einer Kopfzeile auf einfache Leerzeichen normalisieren."""
    return _HORIZONTAL_SPACE.sub(" ", value.replace("\r", " ").replace("\n", " ")).strip()


def _normalize_text(value: str) -> str:
    """Zeilenenden vereinheitlichen, Leerraum aufraeumen."""
    text = _CRLF.sub("\n", value)
    text = _TRAILING_SPACE.sub("\n", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


# ----------------------------------------------------------------------
# HTML -> Text
# ----------------------------------------------------------------------

# Tags, deren Inhalt kein Fliesstext ist.
_SKIP_TAGS: Final[frozenset[str]] = frozenset(
    {"script", "style", "head", "title", "noscript", "svg"}
)

# Tags, die im Text einen Zeilenumbruch erzeugen.
_BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
)

# Tags, die im Text lediglich ein Leerzeichen erzeugen — sonst kleben
# Tabellenzellen aneinander („Sendungsnummer00340…").
_SPACING_TAGS: Final[frozenset[str]] = frozenset({"td", "th", "span"})

# Nur echte Web-Links sind als Fundstelle interessant.
_LINK_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://")

# Sehr lange Links (Newsletter-Weiterleitungen mit eingebetteten Tokens) blaehen
# den Text auf, ohne je eine Trackingnummer zu tragen.
MAX_LINK_LENGTH: Final = 400

_TAGS: Final = re.compile(r"<[^>]*>")


class _TextExtractor(HTMLParser):
    """Sammelt aus HTML lesbaren Text und behaelt Links als `Text (URL)`.

    Bewusst ueber den Parser der Standardbibliothek statt per Regex: Mail-HTML ist
    haeufig unvollstaendig verschachtelt, und `HTMLParser` kommt damit klaglos
    zurecht. Eine zusaetzliche Abhaengigkeit muesste durch HACS und hassfest.
    """

    def __init__(self) -> None:
        """Parser im nachsichtigen Modus mit aufgeloesten Entities anlegen."""
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._links: list[tuple[str, int]] = []

    def result(self) -> str:
        """Den gesammelten Text als Ergebnis aufbereiten."""
        lines = [
            line.strip() for line in _HORIZONTAL_SPACE.sub(" ", "".join(self._parts)).split("\n")
        ]
        return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Umbrueche setzen, Links merken, uninteressante Bereiche ueberspringen."""
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "img":
            # Zaehlpixel und Layout-Grafiken tragen keinen Text.
            return
        if tag == "a":
            # Trenner vor dem Linktext, sonst klebt er am vorherigen Wort. Erst
            # danach die Startposition merken, damit sie auf den Text zeigt.
            self._parts.append(" ")
            href = next((value for name, value in attrs if name == "href"), None)
            self._links.append((href or "", len(self._parts)))
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in _SPACING_TAGS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        """Links abschliessen und Bloecke umbrechen."""
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a":
            self._close_link()
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Fliesstext uebernehmen."""
        if not self._skip_depth:
            self._parts.append(data)

    def _close_link(self) -> None:
        """Die URL hinter den Linktext schreiben, sofern sie etwas beitraegt."""
        if not self._links:
            return
        href, start = self._links.pop()
        url = href.strip()
        if not url.lower().startswith(_LINK_SCHEMES) or len(url) > MAX_LINK_LENGTH:
            return
        label = "".join(self._parts[start:]).strip()
        if url in label:
            return
        self._parts.append(f" ({url})" if label else f" {url} ")


def html_to_text(html: str) -> str:
    """HTML in lesbaren Text ueberfuehren; Links bleiben als `Text (URL)` erhalten."""
    if not html:
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # lieber grober Text als gar keiner
        return _normalize_text(_TAGS.sub(" ", html))
    return extractor.result()


# ----------------------------------------------------------------------
# MIME
# ----------------------------------------------------------------------


def _collect_bodies(message: Message) -> tuple[list[str], list[str]]:
    """Alle Text- und HTML-Teile einer Nachricht einsammeln (ohne Anhaenge)."""
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # defekte Kodierung kostet nur diesen Teil
            continue
        if not payload:
            continue
        (plain if content_type == "text/plain" else html).append(
            decode_bytes(payload, part.get_content_charset())
        )
    return plain, html


def _parse_date(value: object) -> datetime | None:
    """Das `Date`-Feld lesen; unbrauchbare Angaben ergeben `None`."""
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except TypeError, ValueError:
        return None
    if parsed is None:
        return None
    # Mails ohne Zonenangabe gibt es; UTC ist die einzig sinnvolle Annahme.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _recipients(message: Message) -> tuple[str, ...]:
    """Alle Empfaengeradressen in Kleinbuchstaben, ohne Dubletten."""
    raw: list[str] = []
    for name in _RECIPIENT_HEADERS:
        for header in message.get_all(name) or []:
            raw.append(decode_header_value(header))
    addresses = [address.strip().lower() for _, address in getaddresses(raw) if "@" in address]
    return tuple(dict.fromkeys(addresses))


def _fallback_message_id(raw: bytes) -> str:
    """Ersatz-ID fuer Mails ohne `Message-ID` — stabil ueber den Inhalt."""
    digest = hashlib.sha1(raw, usedforsecurity=False).hexdigest()
    return f"<{digest}@delivery-tracker.invalid>"


def parse_mail(raw: bytes, *, uid: int | None = None, folder: str | None = None) -> MailMessage:
    """Eine Rohmail in das Format bringen, mit dem Carrier-Plugins arbeiten.

    Die Funktion ist bewusst total: Auch aus Bruchstuecken entsteht ein Objekt,
    notfalls mit leerem Body. Aufrufer muessen nur `MailMessage.searchable_text`
    auf Leere pruefen, keine Exceptions fangen.
    """
    message = message_from_bytes(raw)

    plain_parts, html_parts = _collect_bodies(message)
    text = _normalize_text("\n\n".join(plain_parts))[:MAX_BODY_LENGTH]
    html = "\n".join(html_parts)[:MAX_BODY_LENGTH] or None

    if html is not None and len(text) < MIN_USEFUL_TEXT:
        # Reine HTML-Mail oder ein Text-Teil, der nur auf HTML verweist.
        text = html_to_text(html)[:MAX_BODY_LENGTH]

    from_name, from_address = parseaddr(decode_header_value(message.get("From")))

    return MailMessage(
        message_id=decode_header_value(message.get("Message-ID")) or _fallback_message_id(raw),
        subject=decode_header_value(message.get("Subject")),
        text=text,
        html=html,
        from_address=from_address.strip().lower(),
        from_name=_collapse(from_name) or None,
        to_addresses=_recipients(message),
        date=_parse_date(message.get("Date")),
        list_unsubscribe=decode_header_value(message.get("List-Unsubscribe")) or None,
        uid=uid,
        folder=folder,
    )
