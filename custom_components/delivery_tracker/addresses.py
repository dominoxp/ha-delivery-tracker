"""Zuordnung Sendung -> konfigurierte Zieladresse.

Jede Sendung landet bei genau einem Zielort: bei der konfigurierten Adresse mit
der hoechsten Bewertung oder, wenn keine den Mindestwert erreicht, beim
eingebauten Zielort „Unbekannt".

Die Bewertung ist additiv. Jede Regelart einer Adresse (Name, Strasse, PLZ, ...)
zaehlt hoechstens einmal; mehrere Schreibweisen derselben Art sind Alternativen,
keine Bonuspunkte. Bei Gleichstand entscheidet die konfigurierte Reihenfolge.

Ausgewertet wird nur, was an der Sendung gespeichert ist — `destination_raw` und
`recipient_emails`, nie die Mail selbst. Nur so laesst sich die Zuordnung bei
jedem Durchlauf neu bewerten: Aendert der Nutzer eine Regel, zieht eine
bestehende Sendung um, obwohl ihre Mail laengst verarbeitet ist.

Der Abholort (`pickup_location`) spielt bewusst keine Rolle. Eine Sendung, die
beim Nachbarn oder im Paketshop liegt, gehoert weiter zu ihrer Zieladresse.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from .const import DESTINATION_UNKNOWN
from .mail import MailMessage
from .models import Destination, Shipment

_LOGGER = logging.getLogger(__name__)

# Gewichte je Regelart. Automaten- und Postnummer sind praktisch eindeutig, der
# Ort allein dagegen kaum: Er steht auch in Shop- und Filialnamen und teilt sich
# meist mehrere Zielorte.
WEIGHT_PACKSTATION: Final = 40
WEIGHT_POST_NUMBER: Final = 40
WEIGHT_STREET: Final = 30
WEIGHT_EMAIL: Final = 30
WEIGHT_RECIPIENT_NAME: Final = 20
WEIGHT_POSTAL_CODE: Final = 20
WEIGHT_KEYWORD: Final = 20
WEIGHT_CITY: Final = 10

# Mindestwert fuer eine Zuordnung. Jeder einzelne Treffer ausser dem Ort reicht —
# viele Zusteller nennen nur Postleitzahl und Ort.
MIN_SCORE: Final = 15

# Obergrenze fuer eine aus dem Mailtext gezogene Adresszeile.
MAX_RAW_LENGTH: Final = 200
# Wie viele Zeilen ein Adressblock unter einer alleinstehenden Ueberschrift hat.
MAX_BLOCK_LINES: Final = 4
MAX_BLOCK_LINE_LENGTH: Final = 80


# ----------------------------------------------------------------------
# Toleranter Textvergleich
# ----------------------------------------------------------------------

_UMLAUTS: Final = str.maketrans({"ä": "a", "ö": "o", "ü": "u"})
# Umschreibungen auf denselben Grundbuchstaben wie die Umlaute: „Mueller" und
# „Müller" werden beide zu „muller". Beidseitig angewandt, kostet das nichts.
_DIGRAPHS: Final = re.compile(r"([aou])e")
_NON_ALNUM: Final = re.compile(r"[^a-z0-9]+")
# „Muster Str", „Muster-Straße" und „Musterstr." sind dieselbe Strasse.
_SPLIT_SUFFIX: Final = re.compile(r"(?<=[a-z]) (?=(?:strasse|str|platz|pl)\b)")
_STREET: Final = re.compile(r"strasse\b")
_SQUARE: Final = re.compile(r"platz\b")
# „1 a" und „1a" sind dieselbe Hausnummer.
_HOUSE_NUMBER_SUFFIX: Final = re.compile(r"(?<=\d) (?=[a-z]\b)")


def normalize(value: str) -> str:
    """Text auf die Form bringen, in der Adressen verglichen werden.

    Kleinschreibung, keine Umlaute und Akzente, keine Satzzeichen, einheitliche
    Abkuerzungen, einfache Leerzeichen. „Musterstraße 1 A" und „musterstr. 1a"
    ergeben dasselbe.
    """
    text = value.casefold().translate(_UMLAUTS)
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
    )
    # Die Umschreibungen erst nach dem Wegfall der Satzzeichen ersetzen, damit
    # „Straße" (-> strasse) und „Strasse" denselben Weg nehmen.
    text = _NON_ALNUM.sub(" ", text).strip()
    text = _DIGRAPHS.sub(r"\1", text)
    text = _SPLIT_SUFFIX.sub("", text)
    text = _STREET.sub("str", text)
    text = _SQUARE.sub("pl", text)
    return _HOUSE_NUMBER_SUFFIX.sub("", text)


def _digits(value: str) -> str:
    """Nur die Ziffern eines Wertes — fuer PLZ, Automaten- und Postnummern."""
    return "".join(char for char in value if char.isdigit())


def normalize_email(value: str) -> str:
    """Mailadresse vergleichbar machen; „erika+pakete@x.de" ist „erika@x.de"."""
    address = value.strip().strip("<>").lower()
    local, separator, domain = address.rpartition("@")
    if not separator:
        return address
    local = local.split("+", 1)[0]
    return f"{local}@{domain}"


# ----------------------------------------------------------------------
# Bewertung
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AddressContext:
    """Was ueber das Ziel einer Sendung bekannt ist, fuer den Vergleich aufbereitet."""

    text: str
    """Normalisierte Adresszeile, mit Leerzeichen eingerahmt fuer Wortgrenzen."""
    tokens: frozenset[str]
    emails: frozenset[str]

    @classmethod
    def build(cls, raw: str | None, emails: Iterable[str] = ()) -> AddressContext:
        """Kontext aus Rohadresse und Empfaengeradressen bauen."""
        text = normalize(raw or "")
        return cls(
            text=f" {text} ",
            tokens=frozenset(text.split()),
            emails=frozenset(normalize_email(email) for email in emails if email),
        )

    @property
    def is_empty(self) -> bool:
        """True, wenn es nichts zu vergleichen gibt."""
        return not self.tokens and not self.emails

    def contains(self, phrase: str) -> bool:
        """True, wenn die normalisierte Wortfolge als Ganzes vorkommt."""
        return bool(phrase) and f" {phrase} " in self.text


@dataclass(frozen=True, slots=True)
class DestinationMatch:
    """Ergebnis der Bewertung einer Sendung gegen einen Zielort."""

    destination_id: str
    score: int = 0
    hits: tuple[str, ...] = ()
    """Getroffene Regelarten — fuer Diagnostics und das Nachvollziehen im Log."""

    @property
    def is_unknown(self) -> bool:
        """True beim eingebauten Zielort „Unbekannt"."""
        return self.destination_id == DESTINATION_UNKNOWN


UNKNOWN_MATCH: Final = DestinationMatch(DESTINATION_UNKNOWN)


def _postal_code_variants(code: str) -> tuple[str, ...]:
    """„12345" auch mit einem Leerzeichen an beliebiger Stelle („12 345")."""
    return (code, *(f"{code[:split]} {code[split:]}" for split in range(1, len(code))))


def _packstation_pattern(number: str) -> re.Pattern[str]:
    """Automaten- oder Filialnummer, aber nur hinter dem passenden Wort.

    Eine dreistellige Zahl allein ist keine Packstation — sie steht in jeder
    zweiten Hausnummer.
    """
    return re.compile(rf" [a-z]*(?:station|filial[a-z]*|locker)(?: nr| nummer| no)? {number} ")


class _DestinationRules:
    """Die Match-Regeln eines Zielorts, einmal normalisiert."""

    def __init__(self, destination: Destination) -> None:
        """Regeln aus der Konfiguration vorbereiten; leere Eintraege fallen weg."""
        self.destination = destination
        self.names = _clean_list(normalize(value).split() for value in destination.recipient_names)
        self.streets = _clean_list(normalize(value) for value in destination.streets)
        self.cities = _clean_list(normalize(value) for value in destination.cities)
        self.keywords = _clean_list(normalize(value) for value in destination.keywords)
        self.postal_codes = _clean_list(_digits(value) for value in destination.postal_codes)
        self.post_numbers = _clean_list(_digits(value) for value in destination.post_numbers)
        self.emails = _clean_list(normalize_email(value) for value in destination.emails)
        self.packstations: list[re.Pattern[str] | str] = []
        for value in destination.packstations:
            if number := _digits(value):
                self.packstations.append(_packstation_pattern(number))
            elif phrase := normalize(value):
                self.packstations.append(phrase)

    def evaluate(self, context: AddressContext) -> DestinationMatch:
        """Die Sendung gegen diese Regeln bewerten."""
        if self._has_postal_code(context):
            postal_score = WEIGHT_POSTAL_CODE
        elif self._other_postal_code(context):
            # Eine ausdruecklich andere Postleitzahl schliesst diesen Zielort aus —
            # sonst gewaenne er allein ueber den Namen, den alle Zielorte teilen.
            return DestinationMatch(self.destination.id)
        else:
            postal_score = 0

        scores = {
            "recipient_name": max(
                (
                    WEIGHT_RECIPIENT_NAME + len(tokens)
                    for tokens in self.names
                    if set(tokens) <= context.tokens
                ),
                default=0,
            ),
            "street": _phrase_score(WEIGHT_STREET, self.streets, context),
            "city": _phrase_score(WEIGHT_CITY, self.cities, context),
            "keyword": _phrase_score(WEIGHT_KEYWORD, self.keywords, context),
            "postal_code": postal_score,
            "packstation": WEIGHT_PACKSTATION * self._has_packstation(context),
            "post_number": WEIGHT_POST_NUMBER
            * any(context.contains(number) for number in self.post_numbers),
            "email": WEIGHT_EMAIL
            * any(_email_matches(rule, context.emails) for rule in self.emails),
        }
        hits = tuple(sorted(kind for kind, score in scores.items() if score))
        return DestinationMatch(self.destination.id, sum(scores.values()), hits)

    def _has_postal_code(self, context: AddressContext) -> bool:
        """True, wenn eine eigene Postleitzahl vorkommt, auch als „12 345"."""
        return any(
            context.contains(variant)
            for code in self.postal_codes
            for variant in _postal_code_variants(code)
        )

    def _has_packstation(self, context: AddressContext) -> bool:
        """True, wenn eine eigene Automaten- oder Filialnummer vorkommt."""
        return any(
            rule.search(context.text) if isinstance(rule, re.Pattern) else context.contains(rule)
            for rule in self.packstations
        )

    def _other_postal_code(self, context: AddressContext) -> bool:
        """True, wenn die Adresszeile eine Postleitzahl traegt, aber keine eigene."""
        lengths = {len(code) for code in self.postal_codes}
        return any(token.isdigit() and len(token) in lengths for token in context.tokens)


def _clean_list[T](values: Iterable[T]) -> list[T]:
    """Leere Regeln verwerfen — ein leerer Eintrag traefe sonst jede Sendung."""
    return [value for value in values if value]


def _phrase_score(weight: int, phrases: Iterable[str], context: AddressContext) -> int:
    """Bester Treffer einer Regelart; mehrere Schreibweisen zaehlen nicht doppelt.

    Jedes getroffene Wort zaehlt einen Punkt extra: So schlaegt „Neue
    Musterstraße 1" die „Musterstraße 1", wenn beide in derselben Zeile stecken.
    """
    return max(
        (weight + phrase.count(" ") + 1 for phrase in phrases if context.contains(phrase)),
        default=0,
    )


def _email_matches(rule: str, emails: frozenset[str]) -> bool:
    """Eine Adressregel pruefen; „@example.com" steht fuer die ganze Domain."""
    if rule.startswith("@"):
        return any(email.endswith(rule) for email in emails)
    return rule in emails


class AddressMatcher:
    """Ordnet Sendungen den konfigurierten Zielorten zu."""

    def __init__(
        self, destinations: Iterable[Destination] = (), *, min_score: int = MIN_SCORE
    ) -> None:
        """Regeln aller Zielorte vorbereiten; die Reihenfolge entscheidet Gleichstaende."""
        self._min_score = min_score
        self._rules: list[_DestinationRules] = []
        for destination in destinations:
            if destination.id == DESTINATION_UNKNOWN:
                _LOGGER.warning(
                    "Zielort '%s' ueberdeckt den eingebauten Zielort und wird ignoriert",
                    destination.id,
                )
                continue
            self._rules.append(_DestinationRules(destination))

    @property
    def destinations(self) -> list[Destination]:
        """Die beruecksichtigten Zielorte in ihrer Reihenfolge."""
        return [rules.destination for rules in self._rules]

    def evaluate(self, raw: str | None, emails: Iterable[str] = ()) -> list[DestinationMatch]:
        """Alle Zielorte bewerten, in konfigurierter Reihenfolge (fuer Diagnostics)."""
        context = AddressContext.build(raw, emails)
        if context.is_empty:
            return [DestinationMatch(rules.destination.id) for rules in self._rules]
        return [rules.evaluate(context) for rules in self._rules]

    def match(self, raw: str | None, emails: Iterable[str] = ()) -> DestinationMatch:
        """Den besten Zielort bestimmen, notfalls „Unbekannt"."""
        best = UNKNOWN_MATCH
        # `>` statt `>=`: Bei Gleichstand bleibt der zuerst konfigurierte.
        for candidate in self.evaluate(raw, emails):
            if candidate.score >= self._min_score and candidate.score > best.score:
                best = candidate
        return best

    def match_shipment(self, shipment: Shipment) -> DestinationMatch:
        """Eine Sendung anhand ihrer gespeicherten Adressdaten bewerten.

        Ein vom Nutzer festgelegter Zielort gewinnt, solange es ihn gibt.
        """
        override = shipment.destination_override
        if override and any(rules.destination.id == override for rules in self._rules):
            return DestinationMatch(override, hits=("override",))
        return self.match(shipment.destination_raw, shipment.recipient_emails)

    def assign(self, shipment: Shipment) -> bool:
        """`destination_id` neu setzen. True, wenn die Sendung umgezogen ist.

        `destination_raw` bleibt in jedem Fall unangetastet.
        """
        destination_id = self.match_shipment(shipment).destination_id
        if shipment.destination_id == destination_id:
            return False
        shipment.destination_id = destination_id
        return True

    def assign_all(self, shipments: Iterable[Shipment]) -> list[Shipment]:
        """Alle Sendungen neu bewerten und die umgezogenen zurueckgeben."""
        return [shipment for shipment in shipments if self.assign(shipment)]

    def __repr__(self) -> str:
        """Kurzform fuer Logs und Diagnostics."""
        return f"<AddressMatcher zielorte={[rules.destination.id for rules in self._rules]}>"


# ----------------------------------------------------------------------
# Rueckfallebene: Adresse aus dem Mailtext ziehen
# ----------------------------------------------------------------------

_LABELS: Final = (
    r"(?:liefer|zustell|versand|empf[aä]nger)(?:adresse|anschrift)",
    r"empf[aä]nger(?:in)?",
    r"(?:zustellung|lieferung|versand) an",
    r"wird (?:geliefert|zugestellt) an",
    r"(?:delivery|shipping) address",
    r"ship(?:ping)? to",
    r"deliver(?:y|ing) to",
)

# Ueberschrift mit Doppelpunkt oder allein am Zeilenende. Ohne diese Bedingung
# waere „Der Empfänger war nicht anzutreffen" eine Adresse.
_LABEL: Final = re.compile(
    rf"(?<![^\W\d_])(?:{'|'.join(_LABELS)})[^\S\n]*(?::[^\S\n]*(?P<rest>[^\n]*)|$)",
    re.IGNORECASE | re.MULTILINE,
)

# Ein Abholort ist keine Zieladresse — er darf die Zuordnung nicht verfaelschen.
_PICKUP_HINT: Final = re.compile(r"nachbar|paket ?shop|parcel ?shop|abhol|pick ?up", re.IGNORECASE)

# Naechste Feldzeile („Sendungsnummer: …") beendet einen Adressblock.
_FIELD_LINE: Final = re.compile(r"^[^\d:]{2,30}:")

_POST_NUMBER: Final = re.compile(r"postnummer:?[^\S\n]*(?P<value>\d[\d ]{4,14}\d)", re.IGNORECASE)

_SPACES: Final = re.compile(r"\s+")


def _block_after(text: str, start: int) -> list[str]:
    """Die Zeilen eines Adressblocks unter einer alleinstehenden Ueberschrift."""
    lines: list[str] = []
    for line in text[start:].lstrip("\n").split("\n"):
        line = line.strip()
        if not line or _FIELD_LINE.match(line) or "http" in line.lower():
            break
        if len(line) > MAX_BLOCK_LINE_LENGTH:
            break
        lines.append(line)
        if len(lines) >= MAX_BLOCK_LINES:
            break
    return lines


def _clip(value: str) -> str:
    """Whitespace normalisieren und die Laenge deckeln."""
    return _SPACES.sub(" ", value).strip(" ,;")[:MAX_RAW_LENGTH]


def extract_destination_raw(text: str) -> str | None:
    """Die Zieladresse aus einem Mailtext ziehen, wenn das Plugin keine kennt.

    Gesucht wird nur hinter ausdruecklichen Ueberschriften wie „Lieferadresse:".
    Eine freie Suche nach „PLZ Ort" faende vor allem das Impressum des Zustellers.
    Die Postnummer kommt dazu, weil sie bei Packstation-Sendungen oft getrennt
    von der Adresse genannt wird.
    """
    address: str | None = None
    for match in _LABEL.finditer(text):
        rest = (match.group("rest") or "").strip()
        lines = [rest] if len(rest) >= 3 else _block_after(text, match.end())
        candidate = _clip(", ".join(lines))
        if len(candidate) < 3 or _PICKUP_HINT.search(candidate):
            continue
        address = candidate
        break

    if (post := _POST_NUMBER.search(text)) is not None:
        number = _digits(post.group("value"))
        if address is None:
            address = f"Postnummer {number}"
        elif number not in _digits(address):
            address = _clip(f"{address}, Postnummer {number}")

    return address


def enrich_from_mail(shipments: Sequence[Shipment], mail: MailMessage) -> None:
    """Adressdaten der Mail an die Sendungen haengen, ohne Plugin-Werte zu ersetzen.

    `destination_raw` kommt nur dazu, wenn das Plugin keine Adresse geliefert
    hat; die Empfaengeradressen kennt nur die Mail selbst.
    """
    extracted: str | None = None
    if any(shipment.destination_raw is None for shipment in shipments):
        extracted = extract_destination_raw(mail.searchable_text)
    for shipment in shipments:
        if shipment.destination_raw is None:
            shipment.destination_raw = extracted
        for address in mail.to_addresses:
            if address not in shipment.recipient_emails:
                shipment.recipient_emails.append(address)


# ----------------------------------------------------------------------
# Vorschlag: aus einer erkannten Adresse einen Zielort machen
# ----------------------------------------------------------------------

_SUGGEST_TAG: Final = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
# Teile einer Adresszeile: Komma, Semikolon, Zeilenumbruch oder ein freistehender Strich.
_SUGGEST_SEPARATOR: Final = re.compile(r"\s*[,;\n]\s*|\s+[-\u2013\u2014|]\s+")
_SUGGEST_POSTAL_CITY: Final = re.compile(
    r"(?<!\d)(?:D-)?(?P<code>\d{5})(?!\d)(?:\s+(?P<city>[^\W\d_][\w.\-]*(?:\s[^\W\d_][\w.\-]*)?))?"
)
_SUGGEST_STREET: Final = re.compile(
    r"(?P<street>(?:[^\W\d_][\w.\-]*\s)*?[^\W\d_][\w\-]*[\s\-]?"
    r"(?:str\.|stra(?:ß|ss)e|weg|platz|allee|gasse|ring|damm|ufer|chaussee)"
    r"\s?\d+(?:\s?[a-zA-Z](?![\w]))?)",
    re.IGNORECASE,
)
_SUGGEST_PACKSTATION: Final = re.compile(
    r"(?P<label>packstation|postfiliale|filiale|paket ?shop|parcel ?shop|locker)"
    r"(?:\s*(?:nr\.?|nummer|no\.?))?\s*(?P<number>\d{3,})",
    re.IGNORECASE,
)
_SUGGEST_LOCATION_LABEL: Final = re.compile(
    r"packstation|filiale|paket ?shop|parcel ?shop|locker|abholstation", re.IGNORECASE
)
_SUGGEST_POST_NUMBER: Final = re.compile(
    r"postnummer:?\s*(?P<number>\d[\d ]{4,14}\d)", re.IGNORECASE
)
# Kurze Reste wie Bundesland-Kuerzel („HE") taugen nicht als Schluesselwort.
_SUGGEST_MIN_KEYWORD: Final = 4


@dataclass(frozen=True, slots=True)
class DestinationSuggestion:
    """Vorbelegung fuer einen neuen Zielort; der Nutzer kann alles noch aendern."""

    name: str
    rules: dict[str, list[str]]


def suggest_destination(text: str | None) -> DestinationSuggestion:
    """Name und Regeln aus einer Adresszeile ableiten.

    Bewusst grob, denn alles bleibt im Formular aenderbar. Gesucht werden
    Postnummer, Packstation-/Filialnummer, Strasse mit Hausnummer und PLZ mit
    Ort — im ganzen Text, weil Zusteller die Teile mal mit Komma, mal mit
    Strich trennen. Was uebrig bleibt, wird in Teile zerlegt: Der erste ist
    meist Empfaenger oder Ladenname und wird nur Name, die uebrigen werden
    Schluesselwoerter (etwa der Name eines Amazon Lockers). Den eigenen Namen
    als Regel vorzuschlagen, taugte nicht: Er steht in jeder Sendung.
    """
    rules: dict[str, list[str]] = {
        "streets": [],
        "postal_codes": [],
        "cities": [],
        "packstations": [],
        "post_numbers": [],
        "keywords": [],
    }
    rest = _SUGGEST_TAG.sub(" ", text or "")
    label: str | None = None

    def take(pattern: re.Pattern[str], handle: Callable[[re.Match[str]], None]) -> None:
        nonlocal rest
        for match in list(pattern.finditer(rest)):
            handle(match)
        rest = pattern.sub(" ", rest)

    def add(key: str, value: str | None) -> None:
        value = (value or "").strip(" .,")
        if value and value.casefold() not in (item.casefold() for item in rules[key]):
            rules[key].append(value)

    def on_station(match: re.Match[str]) -> None:
        nonlocal label
        add("packstations", match.group("number"))
        label = label or f"{match.group('label').title()} {match.group('number')}"

    take(_SUGGEST_POST_NUMBER, lambda match: add("post_numbers", _digits(match.group("number"))))
    take(_SUGGEST_PACKSTATION, on_station)
    take(_SUGGEST_STREET, lambda match: add("streets", match.group("street")))

    def on_place(match: re.Match[str]) -> None:
        add("postal_codes", match.group("code"))
        add("cities", match.group("city"))

    take(_SUGGEST_POSTAL_CITY, on_place)

    parts = [
        part.strip(" .")
        for part in _SUGGEST_SEPARATOR.split(_SPACES.sub(" ", rest))
        if part.strip(" .")
    ]
    first, others = (parts[0], parts[1:]) if parts else (None, [])
    for part in others:
        if len(part) >= _SUGGEST_MIN_KEYWORD:
            add("keywords", part)

    name = label
    if name is None and first and _SUGGEST_LOCATION_LABEL.search(first) and rules["keywords"]:
        # „Amazon Locker - Lindenhof": erst der Zusatz macht den Namen eindeutig.
        name = f"{first} {rules['keywords'][0]}"
    name = name or first or next(iter(rules["streets"]), None)
    return DestinationSuggestion(name=name or "", rules=rules)
