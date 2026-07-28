"""Laden und Validieren der deklarativen Carrier-Regeln (`rules.yaml`).

Jedes Carrier-Plugin legt neben sein `__init__.py` eine `rules.yaml`. Sie ist die
Nutzerschnittstelle zum Erweitern: Wer einen Zusteller ergaenzen will, soll im
Regelfall nur diese Datei schreiben muessen.

Ungueltige Regeln scheitern laut — mit Dateipfad und Fundstelle — statt still
ignoriert zu werden. Alle Regex werden beim Laden einmalig kompiliert (immer
`re.IGNORECASE`, weil Mailtexte in Gross- und Kleinschreibung variieren).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import voluptuous as vol
import yaml
from homeassistant.exceptions import HomeAssistantError
from voluptuous.humanize import humanize_error

from ..models import ShipmentStatus
from ..tracking import TrackingChecksum

RULES_FILENAME: Final = "rules.yaml"

# Felder, die sich rein deklarativ aus dem Mailtext ziehen lassen.
EXTRACT_FIELDS: Final[tuple[str, ...]] = (
    "sender",
    "description",
    "order_number",
    "pickup_location",
    "pickup_code",
    "destination_raw",
)

# Platzhalter, den jede Tracking-URL-Vorlage enthalten muss.
_URL_PLACEHOLDER: Final = "{tracking_number}"

_CARRIER_ID: Final = re.compile(r"^[a-z][a-z0-9_]*$")


class RulesError(HomeAssistantError):
    """Eine `rules.yaml` ist fehlerhaft und das Plugin damit unbrauchbar."""


def _regex(value: Any) -> re.Pattern[str]:
    """Einen regulaeren Ausdruck kompilieren."""
    if not isinstance(value, str):
        raise vol.Invalid(f"Regex muss ein Text sein, nicht {type(value).__name__}")
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as err:
        raise vol.Invalid(f"ungueltiger regulaerer Ausdruck {value!r}: {err}") from err


def _regex_list(value: Any) -> list[re.Pattern[str]]:
    """Eine Liste von Regex; ein einzelner Wert wird eingepackt."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [_regex(item) for item in items]


def _domain(value: Any) -> str:
    """Eine Absender-Domain normalisieren."""
    text = str(value).strip().lower().lstrip("@").rstrip(".")
    if not text or "." not in text:
        raise vol.Invalid(f"{value!r} ist keine gueltige Domain")
    return text


def _domain_list(value: Any) -> list[str]:
    """Eine Liste von Absender-Domains."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [_domain(item) for item in items]


def _status(value: Any) -> ShipmentStatus:
    """Einen Statusnamen auf den normalisierten Enum abbilden."""
    try:
        return ShipmentStatus(str(value))
    except ValueError as err:
        allowed = ", ".join(status.value for status in ShipmentStatus)
        raise vol.Invalid(f"unbekannter Status {value!r} (erlaubt: {allowed})") from err


def _carrier_id(value: Any) -> str:
    """Die Carrier-ID pruefen — sie steckt in jeder Sendungs-ID.

    Bewusst streng und ohne stille Korrektur: Die ID muss in der Datei genau so
    stehen, wie sie spaeter in jeder Sendungs-ID auftaucht.
    """
    text = str(value).strip()
    if not _CARRIER_ID.match(text):
        raise vol.Invalid(
            f"Carrier-ID {value!r} muss mit einem Kleinbuchstaben beginnen und nur "
            "Kleinbuchstaben, Ziffern und Unterstriche enthalten"
        )
    return text


def _checksum(value: Any) -> TrackingChecksum:
    """Das Pruefziffernverfahren des Zustellers auswaehlen."""
    try:
        return TrackingChecksum(str(value))
    except ValueError as err:
        allowed = ", ".join(scheme.value for scheme in TrackingChecksum)
        raise vol.Invalid(f"unbekanntes Pruefverfahren {value!r} (erlaubt: {allowed})") from err


def _url_template(value: Any) -> str | None:
    """Die Tracking-URL-Vorlage pruefen; `None` bedeutet „keine Vorlage"."""
    if value is None:
        return None
    template = str(value)
    if _URL_PLACEHOLDER not in template:
        raise vol.Invalid(f"Tracking-URL {template!r} muss '{_URL_PLACEHOLDER}' enthalten")
    try:
        template.format(tracking_number="TEST")
    except (KeyError, IndexError, ValueError) as err:
        raise vol.Invalid(
            f"Tracking-URL {template!r} hat einen unbekannten Platzhalter: {err}"
        ) from err
    return template


_MATCH_SCHEMA: Final = vol.Schema(
    {
        vol.Optional("from_domains", default=list): _domain_list,
        vol.Optional("subject_patterns", default=list): _regex_list,
        vol.Optional("body_patterns", default=list): _regex_list,
        vol.Optional("newsletter_patterns", default=list): _regex_list,
    }
)

_TRACKING_SCHEMA: Final = vol.Schema(
    {
        vol.Optional("patterns", default=list): _regex_list,
        vol.Optional("url", default=None): _url_template,
        vol.Optional("checksum", default=TrackingChecksum.AUTO): _checksum,
        vol.Optional("require_context", default=False): bool,
    }
)

_DATES_SCHEMA: Final = vol.Schema(
    {
        vol.Optional("eta_patterns", default=list): _regex_list,
        vol.Optional("delivered_patterns", default=list): _regex_list,
    }
)

_FIELDS_SCHEMA: Final = vol.Schema(
    {vol.Optional(name, default=list): _regex_list for name in EXTRACT_FIELDS}
)

_STATUS_RULE_SCHEMA: Final = vol.Schema(
    {
        vol.Required("status"): _status,
        vol.Required("patterns"): _regex_list,
    }
)

# Voluptuous validiert Defaults nicht mit — deshalb liefern die Sub-Defaults
# gleich das durchvalidierte, vollstaendig gefuellte Dict.
RULES_SCHEMA: Final = vol.Schema(
    {
        vol.Required("carrier"): _carrier_id,
        vol.Required("name"): vol.All(str, vol.Length(min=1)),
        vol.Optional("score_bonus", default=0): vol.All(int, vol.Range(min=-100, max=100)),
        vol.Optional("priority", default=0): vol.All(int, vol.Range(min=-100, max=100)),
        vol.Optional("match", default=lambda: _MATCH_SCHEMA({})): _MATCH_SCHEMA,
        vol.Optional("tracking", default=lambda: _TRACKING_SCHEMA({})): _TRACKING_SCHEMA,
        vol.Optional("status_map", default=list): [_STATUS_RULE_SCHEMA],
        vol.Optional("dates", default=lambda: _DATES_SCHEMA({})): _DATES_SCHEMA,
        vol.Optional("fields", default=lambda: _FIELDS_SCHEMA({})): _FIELDS_SCHEMA,
    }
)


@dataclass(frozen=True, slots=True)
class StatusRule:
    """Eine Zuordnung Mailtext -> normalisierter Status."""

    status: ShipmentStatus
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True, slots=True)
class CarrierRules:
    """Die validierten, vorkompilierten Regeln eines Carriers."""

    carrier_id: str
    name: str
    score_bonus: int = 0
    priority: int = 0
    """Entscheidet bei gleicher Bewertung, welches Plugin die Mail bekommt."""
    from_domains: tuple[str, ...] = ()
    subject_patterns: tuple[re.Pattern[str], ...] = ()
    body_patterns: tuple[re.Pattern[str], ...] = ()
    newsletter_patterns: tuple[re.Pattern[str], ...] = ()
    """Trifft eines dieser Muster, ist die Mail Werbung — das Plugin haelt sich raus."""
    tracking_patterns: tuple[re.Pattern[str], ...] = ()
    tracking_url_template: str | None = None
    checksum: TrackingChecksum = TrackingChecksum.AUTO
    require_context: bool = False
    """Verlangt ein ankuendigendes Wort an der Fundstelle — fuer weite Muster."""
    status_map: tuple[StatusRule, ...] = ()
    eta_patterns: tuple[re.Pattern[str], ...] = ()
    delivered_patterns: tuple[re.Pattern[str], ...] = ()
    fields: Mapping[str, tuple[re.Pattern[str], ...]] = field(default_factory=dict)

    def tracking_url(self, tracking_number: str | None) -> str | None:
        """Die Tracking-URL zu einer Nummer bilden, falls eine Vorlage hinterlegt ist."""
        if not tracking_number or not self.tracking_url_template:
            return None
        return self.tracking_url_template.format(tracking_number=tracking_number)

    def field_patterns(self, name: str) -> tuple[re.Pattern[str], ...]:
        """Die Extraktionsmuster eines Feldes (leer, wenn nicht konfiguriert)."""
        return self.fields.get(name, ())


def _to_rules(data: Mapping[str, Any]) -> CarrierRules:
    """Das validierte Dict in den unveraenderlichen Regel-Container ueberfuehren."""
    match = data["match"]
    tracking = data["tracking"]
    dates = data["dates"]
    fields = data["fields"]

    return CarrierRules(
        carrier_id=data["carrier"],
        name=data["name"],
        score_bonus=data["score_bonus"],
        priority=data["priority"],
        from_domains=tuple(match["from_domains"]),
        subject_patterns=tuple(match["subject_patterns"]),
        body_patterns=tuple(match["body_patterns"]),
        newsletter_patterns=tuple(match["newsletter_patterns"]),
        tracking_patterns=tuple(tracking["patterns"]),
        tracking_url_template=tracking["url"],
        checksum=tracking["checksum"],
        require_context=tracking["require_context"],
        status_map=tuple(
            StatusRule(status=rule["status"], patterns=tuple(rule["patterns"]))
            for rule in data["status_map"]
        ),
        eta_patterns=tuple(dates["eta_patterns"]),
        delivered_patterns=tuple(dates["delivered_patterns"]),
        fields={name: tuple(patterns) for name, patterns in fields.items()},
    )


def parse_rules(raw: Any, *, origin: str = RULES_FILENAME) -> CarrierRules:
    """Bereits eingelesenes YAML validieren. `origin` erscheint in Fehlermeldungen."""
    if not isinstance(raw, dict):
        raise RulesError(
            f"{origin}: erwartet wird eine YAML-Zuordnung, gefunden {type(raw).__name__}"
        )
    try:
        validated = RULES_SCHEMA(raw)
    except vol.Invalid as err:
        raise RulesError(f"{origin}: {humanize_error(raw, err)}") from err
    return _to_rules(validated)


def load_rules(path: Path) -> CarrierRules:
    """Eine `rules.yaml` lesen und validieren (blockierend — im Executor aufrufen)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise RulesError(f"{path}: nicht lesbar ({err})") from err
    except yaml.YAMLError as err:
        raise RulesError(f"{path}: kein gueltiges YAML ({err})") from err
    return parse_rules(raw, origin=str(path))
