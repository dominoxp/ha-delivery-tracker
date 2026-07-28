"""Trackingnummern: Normalisierung, Pruefziffern und Fundstellen.

Der teuerste Fehler dieser Integration ist ein Fehltreffer: Eine Bestell- oder
Rechnungsnummer, die als Sendung im Dashboard landet, verschwindet nie wieder von
selbst. Dieses Modul haelt deshalb drei Filter bereit, die zusammen entscheiden,
ob eine Zeichenfolge als Trackingnummer durchgeht:

1. **Pruefziffer** — wo der Zusteller eine vorsieht, muss sie stimmen. Das ist
   das einzige harte Kriterium; ohne es produziert der generische Fallback aus
   Step 1.4 zu viele Treffer.
2. **Kontext** — was unmittelbar vor der Fundstelle steht. „Sendungsnummer:" ist
   ein Ja, „Rechnungsnummer:" ein Nein. Bestellnummern sehen Trackingnummern zum
   Verwechseln aehnlich; unterscheidbar sind sie nur am Umfeld.
3. **Fundort** — Nummern aus den Parametern eines Sendungsverfolgungs-Links sind
   verlaesslicher als solche aus dem Fliesstext. Bei vielen Zustellern sind sie
   ueberhaupt die einzige Fundstelle.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import parse_qsl, unquote, urlsplit

# Obergrenze der Nummern je Mail. Sammelbestaetigungen enthalten selten mehr als
# eine Handvoll; alles darueber deutet auf ein zu weites Muster hin.
MAX_TRACKING_NUMBERS: Final = 20

# Wie weit vor einer Fundstelle nach Kontextwoertern gesucht wird. Gross genug
# fuer „Ihre Sendungsnummer lautet: …", klein genug, um nicht den Vorsatz zu treffen.
CONTEXT_WINDOW: Final = 60

# Woerter, die eine Trackingnummer ankuendigen.
POSITIVE_CONTEXT: Final[frozenset[str]] = frozenset(
    {
        "sendungsnummer",
        "sendungsnr",
        "sendungsverfolgung",
        "paketnummer",
        "paketnr",
        "trackingnummer",
        "tracking-nummer",
        "tracking number",
        "trackingnr",
        "tracking id",
        "tracking-id",
        "trackingid",
        "tracking code",
        "verfolgungsnummer",
        "frachtbriefnummer",
        "identcode",
        "waybill",
        "airwaybill",
        "awb",
        "nachverfolgen",
        "sendung verfolgen",
        "paket verfolgen",
        "status ihrer sendung",
    }
)

# Woerter, nach denen keine Trackingnummer kommt, sondern etwas, das nur so
# aussieht. Das ist die wichtigste Verteidigung gegen Fehltreffer.
NEGATIVE_CONTEXT: Final[frozenset[str]] = frozenset(
    {
        "bestellnummer",
        "bestellnr",
        "bestell-nr",
        "auftragsnummer",
        "auftragsnr",
        "rechnungsnummer",
        "rechnungsnr",
        "rechnung nr",
        "kundennummer",
        "kundennr",
        "kunden-nr",
        "vertragsnummer",
        "belegnummer",
        "vorgangsnummer",
        "artikelnummer",
        "artikelnr",
        "bestellwert",
        "ust-idnr",
        "umsatzsteuer",
        "steuernummer",
        "iban",
        "telefon",
        "order number",
        "order id",
        "order no",
        "invoice",
        "reference number",
        "gutscheincode",
        "rabattcode",
    }
)

# Query-Parameter, in denen Zusteller ihre Nummern transportieren.
TRACKING_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "trackingnumber",
        "tracking_number",
        "trackingnumbers",
        "trackingid",
        "tracking_id",
        "tracking",
        "track",
        "tracknumbers",
        "tracknum",
        "sendungsnummer",
        "sendungsnr",
        "nummer",
        "piececode",
        "idc",
        "parcelno",
        "parcelnumber",
        "pknr",
        "shipmentid",
        "shipment_id",
        "awb",
        "code",
        "id",
        "packetcode",
        "matchcode",
        "referencenumber",
        "cons",
        "consignmentnumber",
        "trackingnummer",
        "lettercode",
        "hwb",
    }
)

# Trennzeichen, die in Trackingnummern nur der Lesbarkeit dienen: Leerraum,
# Unterstrich und saemtliche Bindestrich-Varianten (auch die typografischen).
_SEPARATORS: Final = re.compile(r"[\s\-_\u2010-\u2015]+")
_TRAILING_PUNCTUATION: Final = ".,;:!?)]}\"'"

_URL: Final = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.IGNORECASE)
_ALNUM: Final = re.compile(r"^[0-9A-Z]+$")

# UPS: `1Z` + 16 Zeichen, das letzte davon die Pruefziffer.
_UPS: Final = re.compile(r"^1Z[0-9A-Z]{16}$")
# DHL-Sendungsnummer: NVE/SSCC mit vorangestelltem „00" — 20 Stellen und damit
# ein Format, das kein anderer hier unterstuetzter Zusteller verwendet.
_DHL_NVE: Final = re.compile(r"^00\d{18}$")


class ChecksumResult(StrEnum):
    """Ergebnis der Pruefziffernpruefung."""

    VALID = "valid"
    """Die Pruefziffer passt — starkes Signal fuer eine echte Nummer."""
    INVALID = "invalid"
    """Die Pruefziffer passt nicht — die Nummer wird verworfen."""
    UNKNOWN = "unknown"
    """Fuer dieses Format ist keine Pruefziffer bekannt; es entscheidet der Kontext."""


class TrackingChecksum(StrEnum):
    """Welches Pruefverfahren ein Carrier-Plugin anwenden laesst."""

    AUTO = "auto"
    """Nur eindeutig erkennbare Formate pruefen (Standard)."""
    NONE = "none"
    """Nicht pruefen — fuer Zusteller ohne Pruefziffer."""
    UPS = "ups"
    DEUTSCHE_POST = "deutsche_post"
    """Leitcode (14 Stellen) und Identcode (12 Stellen), Gewichtung 4/9."""
    GS1 = "gs1"
    """NVE/SSCC, Gewichtung 3/1 von rechts."""


@dataclass(frozen=True, slots=True)
class TrackingCandidate:
    """Eine Fundstelle mit allem, was ueber ihre Glaubwuerdigkeit bekannt ist."""

    number: str
    """Normalisierte Nummer — das, was gespeichert wird."""
    raw: str
    """Fundstelle im Originalwortlaut."""
    checksum: ChecksumResult = ChecksumResult.UNKNOWN
    context: int = 0
    """`1` bei ankuendigendem Wort, `-1` bei ausschliessendem, sonst `0`."""
    from_url: bool = False

    @property
    def is_plausible(self) -> bool:
        """Ob die Fundstelle als Trackingnummer durchgeht.

        Eine falsche Pruefziffer schliesst immer aus. Ein ausschliessendes
        Kontextwort ebenfalls — es sei denn, die Pruefziffer belegt das Gegenteil.
        """
        if self.checksum is ChecksumResult.INVALID:
            return False
        return self.context >= 0 or self.checksum is ChecksumResult.VALID


def normalize_tracking_number(value: str) -> str:
    """Eine Trennzeichen freie Grossbuchstaben-Fassung der Nummer bilden.

    „JJD 0099-8899" und „jjd00998899" sind dieselbe Sendung; ohne diesen Schritt
    waeren es zwei.
    """
    return _SEPARATORS.sub("", value.strip().strip(_TRAILING_PUNCTUATION)).upper()


# ----------------------------------------------------------------------
# Pruefziffern
# ----------------------------------------------------------------------


def _ups_value(char: str) -> int:
    """Zeichenwert im UPS-Verfahren: Ziffern zaehlen selbst, Buchstaben nach Position."""
    if char.isdigit():
        return int(char)
    # A=2, B=3, … I=0, J=1, … (Alphabetposition + 1, modulo 10).
    return (ord(char) - 63) % 10


def ups_check_digit(payload: str) -> int:
    """Pruefziffer einer UPS-Nummer aus den 15 Zeichen hinter `1Z`.

    Stellen an ungerader Position zaehlen einfach, Stellen an gerader Position
    doppelt; die Pruefziffer fuellt die Summe auf das naechste Vielfache von 10 auf.
    """
    total = 0
    for index, char in enumerate(payload):
        value = _ups_value(char)
        total += value * 2 if index % 2 else value
    return (10 - total % 10) % 10


def deutsche_post_check_digit(payload: str) -> int:
    """Pruefziffer von Leitcode und Identcode (Gewichtung 4/9 von links)."""
    total = 0
    for index, char in enumerate(payload):
        total += int(char) * (4 if index % 2 == 0 else 9)
    return (10 - total % 10) % 10


def gs1_check_digit(payload: str) -> int:
    """Pruefziffer nach GS1 (NVE/SSCC): Gewichtung 3/1, von rechts gezaehlt."""
    total = 0
    for index, char in enumerate(reversed(payload)):
        total += int(char) * (3 if index % 2 == 0 else 1)
    return (10 - total % 10) % 10


def _check(payload: str, digit: str, expected: int) -> ChecksumResult:
    """Eine berechnete Pruefziffer mit der gefundenen vergleichen."""
    if not payload or not digit.isdigit():
        return ChecksumResult.UNKNOWN
    return ChecksumResult.VALID if int(digit) == expected else ChecksumResult.INVALID


def _ups_result(number: str) -> ChecksumResult:
    """UPS-Nummer pruefen."""
    if not _UPS.match(number):
        return ChecksumResult.UNKNOWN
    body = number[2:]
    return _check(body[:-1], body[-1], ups_check_digit(body[:-1]))


def _deutsche_post_result(number: str) -> ChecksumResult:
    """Leitcode (14 Stellen) oder Identcode (12 Stellen) pruefen."""
    if not number.isdigit() or len(number) not in (12, 14):
        return ChecksumResult.UNKNOWN
    return _check(number[:-1], number[-1], deutsche_post_check_digit(number[:-1]))


def _gs1_result(number: str) -> ChecksumResult:
    """NVE/SSCC pruefen; fuehrende Nullen aendern das Ergebnis nicht."""
    if not number.isdigit() or len(number) not in (18, 20):
        return ChecksumResult.UNKNOWN
    return _check(number[:-1], number[-1], gs1_check_digit(number[:-1]))


def checksum_result(
    number: str, scheme: TrackingChecksum = TrackingChecksum.AUTO
) -> ChecksumResult:
    """Die Pruefziffer einer normalisierten Nummer bewerten.

    `AUTO` prueft bewusst nur Formate, die keinem anderen Zusteller gehoeren
    koennen: UPS-Nummern beginnen mit `1Z`, DHL-Sendungsnummern sind 20-stellige
    NVE. Zwoelf- und vierzehnstellige Ziffernfolgen nutzen dagegen mehrere
    Zusteller mit unterschiedlichen Verfahren — dort muss das Plugin sein
    Verfahren in der `rules.yaml` benennen, sonst wuerden gueltige Nummern
    faelschlich verworfen.
    """
    if not number or scheme is TrackingChecksum.NONE:
        return ChecksumResult.UNKNOWN
    if scheme is TrackingChecksum.UPS:
        return _ups_result(number)
    if scheme is TrackingChecksum.DEUTSCHE_POST:
        return _deutsche_post_result(number)
    if scheme is TrackingChecksum.GS1:
        return _gs1_result(number)

    if _UPS.match(number):
        return _ups_result(number)
    if _DHL_NVE.match(number):
        return _gs1_result(number)
    return ChecksumResult.UNKNOWN


# ----------------------------------------------------------------------
# Kontext
# ----------------------------------------------------------------------


def _nearest(text: str, words: frozenset[str]) -> int:
    """Ende des letzten Vorkommens eines der Woerter; `-1`, wenn keines vorkommt."""
    best = -1
    for word in words:
        position = text.rfind(word)
        if position >= 0:
            best = max(best, position + len(word))
    return best


def context_score(text: str, start: int) -> int:
    """Das Umfeld einer Fundstelle bewerten.

    Entscheidend ist das *naechstgelegene* Schluesselwort: In „Bestellnummer 4711,
    Sendungsnummer 1Z…" gehoert nur das zweite Wort zur Nummer.
    """
    before = text[max(0, start - CONTEXT_WINDOW) : start].lower()
    if not before:
        return 0
    positive = _nearest(before, POSITIVE_CONTEXT)
    negative = _nearest(before, NEGATIVE_CONTEXT)
    if positive == negative:
        return 0
    return 1 if positive > negative else -1


# ----------------------------------------------------------------------
# Fundstellen
# ----------------------------------------------------------------------


def _value_span(match: re.Match[str]) -> tuple[str, int]:
    """Nummernteil und seine Position im Text (Gruppe `value`, sonst Gruppe 1).

    Die Position zeigt bewusst auf die Nummer und nicht auf den Trefferanfang:
    Muster wie `sendungsnummer:\\s*(?P<value>…)` tragen ihr Kontextwort selbst,
    und das soll bei der Bewertung mitzaehlen statt herauszufallen.
    """
    if match.groupdict().get("value") is not None:
        return match.group("value"), match.start("value")
    if match.lastindex:
        return match.group(1), match.start(1)
    return match.group(0), match.start(0)


def iter_urls(text: str) -> Iterator[str]:
    """Alle Web-Links eines Textes."""
    for match in _URL.finditer(text):
        yield match.group(0).rstrip(_TRAILING_PUNCTUATION)


def iter_url_values(text: str) -> Iterator[tuple[str, bool]]:
    """Kandidaten aus Links: `(Wert, aus einem Tracking-Parameter)`.

    Geliefert werden die Werte der bekannten Query-Parameter und zusaetzlich die
    Pfadsegmente — manche Zusteller haengen die Nummer direkt an den Pfad.
    """
    for url in iter_urls(text):
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        for name, value in parse_qsl(parts.query, keep_blank_values=False):
            if value:
                yield unquote(value), name.lower() in TRACKING_PARAMS
        for segment in parts.path.split("/"):
            if segment:
                yield unquote(segment), False


def iter_candidates(
    text: str,
    patterns: Sequence[re.Pattern[str]],
    *,
    scheme: TrackingChecksum = TrackingChecksum.AUTO,
) -> Iterator[TrackingCandidate]:
    """Alle Fundstellen der Muster in Text und Links, mit ihrer Bewertung.

    Die Reihenfolge folgt der Reihenfolge der Muster — das erste Muster einer
    `rules.yaml` ist damit auch die erste gelieferte Nummer.
    """
    url_values = list(iter_url_values(text))
    for pattern in patterns:
        yield from _candidates_in_text(text, pattern, scheme)
        for value, from_param in url_values:
            yield from _candidates_in_url(value, pattern, scheme, from_param=from_param)


def _candidates_in_text(
    text: str, pattern: re.Pattern[str], scheme: TrackingChecksum
) -> Iterator[TrackingCandidate]:
    """Fundstellen eines Musters im Fliesstext, bewertet nach ihrem Umfeld."""
    for match in pattern.finditer(text):
        raw, position = _value_span(match)
        if number := normalize_tracking_number(raw):
            yield TrackingCandidate(
                number=number,
                raw=raw,
                checksum=checksum_result(number, scheme),
                context=context_score(text, position),
            )


def _candidates_in_url(
    value: str, pattern: re.Pattern[str], scheme: TrackingChecksum, *, from_param: bool
) -> Iterator[TrackingCandidate]:
    """Fundstellen eines Musters in einem Linkbestandteil.

    Ein bekannter Tracking-Parameter ist selbst schon die Ankuendigung — ein
    Kontextwort gibt es in einer URL nicht.
    """
    for match in pattern.finditer(value):
        raw, _ = _value_span(match)
        if number := normalize_tracking_number(raw):
            yield TrackingCandidate(
                number=number,
                raw=value,
                checksum=checksum_result(number, scheme),
                context=1 if from_param else 0,
                from_url=True,
            )


def find_tracking_numbers(
    text: str,
    patterns: Sequence[re.Pattern[str]],
    *,
    scheme: TrackingChecksum = TrackingChecksum.AUTO,
    require_context: bool = False,
    limit: int = MAX_TRACKING_NUMBERS,
) -> list[str]:
    """Alle plausiblen Trackingnummern eines Textes, ohne Dubletten.

    `require_context` verlangt zusaetzlich ein ankuendigendes Wort oder eine
    stimmende Pruefziffer — die Einstellung fuer generische Muster, die sonst
    jede laengere Zeichenfolge einfangen wuerden.
    """
    if not text or not patterns:
        return []

    found: dict[str, TrackingCandidate] = {}
    for candidate in iter_candidates(text, patterns, scheme=scheme):
        if not candidate.is_plausible:
            continue
        if (
            require_context
            and candidate.context <= 0
            and candidate.checksum is not ChecksumResult.VALID
        ):
            continue
        existing = found.get(candidate.number)
        if existing is None or _rank(candidate) > _rank(existing):
            found[candidate.number] = candidate
        if len(found) >= limit:
            break
    return list(found)


def _rank(candidate: TrackingCandidate) -> tuple[int, int]:
    """Wie gut eine Fundstelle belegt ist — entscheidet unter Dubletten."""
    return (1 if candidate.checksum is ChecksumResult.VALID else 0, candidate.context)


def looks_like_tracking_number(value: str) -> bool:
    """Grobe Formpruefung fuer den generischen Fallback aus Step 1.4."""
    number = normalize_tracking_number(value)
    if not 8 <= len(number) <= 35 or not _ALNUM.match(number):
        return False
    return any(char.isdigit() for char in number)
