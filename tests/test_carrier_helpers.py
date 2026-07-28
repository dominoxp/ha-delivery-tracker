"""Tests fuer die geteilten Regel-Hilfen der Carrier-Plugins."""

from collections.abc import Generator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import yaml
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.carriers.base import RulesCarrier
from custom_components.delivery_tracker.carriers.helpers import (
    MALUS_BULK,
    WEIGHT_BODY,
    WEIGHT_FROM_DOMAIN,
    WEIGHT_SUBJECT,
    WEIGHT_TRACKING,
    build_shipment_from_rules,
    build_shipments_from_rules,
    domain_matches,
    first_match,
    map_status,
    score_mail,
)
from custom_components.delivery_tracker.carriers.rules import CarrierRules, parse_rules
from custom_components.delivery_tracker.const import SOURCE_IMAP
from custom_components.delivery_tracker.mail import MailMessage
from custom_components.delivery_tracker.models import ShipmentStatus

BERLIN = ZoneInfo("Europe/Berlin")
MAIL_DATE = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)

RULES_YAML = """
carrier: acme
name: ACME Logistik
match:
  from_domains: ["acme.com"]
  subject_patterns: ["ihre sendung"]
  body_patterns: ["acme logistik gmbh"]
tracking:
  patterns: ["sendungsnummer:?\\\\s*(?P<value>AC\\\\d{8})"]
  url: "https://acme.example/{tracking_number}"
status_map:
  - status: delivered
    patterns: ["wurde zugestellt"]
  - status: out_for_delivery
    patterns: ["ist in zustellung"]
  - status: in_transit
    patterns: ["ist unterwegs"]
dates:
  eta_patterns: ["voraussichtlich:?\\\\s*(?P<value>[^\\\\n]{3,60})"]
fields:
  sender: ["im auftrag von\\\\s*(?P<value>[^\\\\n]+)"]
  description: ["artikel:?\\\\s*(?P<value>[^\\\\n]+)"]
  order_number: ["bestellnummer:?\\\\s*(?P<value>[A-Z0-9-]{5,30})"]
  destination_raw: ["lieferadresse:?\\\\s*(?P<value>[^\\\\n]+)"]
"""


@pytest.fixture(autouse=True)
def berlin_timezone() -> Generator[None]:
    """Liefertermine werden lokal gerechnet."""
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(original)


@pytest.fixture
def rules() -> CarrierRules:
    """Die Testregeln, einmal je Test frisch geladen."""
    return parse_rules(yaml.safe_load(RULES_YAML), origin="test.yaml")


def _mail(**overrides: object) -> MailMessage:
    """Eine typische Zustellmail."""
    values: dict[str, object] = {
        "message_id": "<42@acme.com>",
        "subject": "Ihre Sendung ist unterwegs",
        "text": (
            "Sendungsnummer: AC12345678\n"
            "Voraussichtlich: Donnerstag, 30.07.2026 zwischen 10 und 14 Uhr\n"
            "Im Auftrag von Kaffeeroesterei Nord\n"
            "Artikel: Kaffeebohnen 1kg\n"
            "Lieferadresse: Musterstrasse 1, 12345 Musterstadt\n"
        ),
        "from_address": "versand@acme.com",
        "date": MAIL_DATE,
    }
    values.update(overrides)
    return MailMessage(**values)  # type: ignore[arg-type]


def test_mail_from_domain():
    """Die Absender-Domain wird aus der Adresse gezogen."""
    assert _mail().from_domain == "acme.com"
    assert MailMessage(message_id="x", from_address="").from_domain == ""


@pytest.mark.parametrize(
    ("domain", "erwartet"),
    [
        ("acme.com", True),
        ("mail.acme.com", True),
        ("notacme.com", False),
        ("acme.com.evil.test", False),
        ("", False),
    ],
)
def test_domain_matches(domain: str, erwartet: bool):
    """Sub-Domains zaehlen mit, aehnlich klingende Domains nicht."""
    assert domain_matches(domain, ["acme.com"]) is erwartet


def test_score_addiert_alle_signale(rules: CarrierRules):
    """Je mehr Signale, desto hoeher die Bewertung."""
    mail = _mail(text=_mail().text + "ACME Logistik GmbH\n")

    assert score_mail(rules, mail) == (
        WEIGHT_FROM_DOMAIN + WEIGHT_SUBJECT + WEIGHT_TRACKING + WEIGHT_BODY
    )


def test_score_null_ohne_signal(rules: CarrierRules):
    """Eine fremde Mail bekommt keine Punkte."""
    fremd = _mail(subject="Newsletter", text="nichts dergleichen", from_address="a@dhl.de")

    assert score_mail(rules, fremd) == 0


def test_absender_allein_reicht_nicht(rules: CarrierRules):
    """Newsletter kommen von derselben Adresse wie Versandbenachrichtigungen.

    Ohne inhaltliches Signal darf die Domain allein deshalb nie einen Treffer
    ergeben — sonst landet jede Werbemail des Zustellers in der Pipeline.
    """
    werbung = _mail(subject="Unsere Angebote", text="Diese Woche 20% Rabatt auf alles.")

    assert score_mail(rules, werbung) == 0


def test_newsletter_muster_setzt_hart_auf_null():
    """Ein Werbe-Muster schlaegt jedes andere Signal."""
    regeln = parse_rules(
        yaml.safe_load(
            "carrier: acme\nname: ACME\n"
            'match:\n  from_domains: ["acme.com"]\n'
            '  subject_patterns: ["ihre sendung"]\n'
            '  newsletter_patterns: ["angebot der woche"]\n'
        )
    )

    assert score_mail(regeln, _mail(text="Angebot der Woche: Kaffee")) == 0


def test_massenversand_ohne_nummer_wird_abgewertet(rules: CarrierRules):
    """`List-Unsubscribe` und keine Trackingnummer — das riecht nach Newsletter."""
    ohne_nummer = _mail(text="Wir informieren Sie, sobald es losgeht.")
    massenversand = _mail(
        text="Wir informieren Sie, sobald es losgeht.",
        list_unsubscribe="<https://acme.com/abmelden>",
    )

    assert score_mail(rules, ohne_nummer) == WEIGHT_FROM_DOMAIN + WEIGHT_SUBJECT
    assert score_mail(rules, massenversand) == WEIGHT_FROM_DOMAIN + WEIGHT_SUBJECT - MALUS_BULK


def test_score_bonus_verschiebt_aber_kippt_nicht():
    """Der Bonus rangiert Plugins, macht aus einem Treffer aber nie ein Nichts."""
    schwach = parse_rules(
        yaml.safe_load(
            "carrier: generic\nname: Generisch\nscore_bonus: -100\n"
            'match:\n  body_patterns: ["sendungsnummer"]\n'
        )
    )

    assert score_mail(schwach, _mail()) == 1


def test_map_status_erste_regel_gewinnt(rules: CarrierRules):
    """Ausgewertet wird in der Reihenfolge der Regeldatei."""
    status, text = map_status("Ihre Sendung wurde zugestellt und ist unterwegs", rules.status_map)

    assert status is ShipmentStatus.DELIVERED
    assert text == "wurde zugestellt"


def test_map_status_ohne_treffer(rules: CarrierRules):
    """Ohne passende Regel bleibt der Status unbekannt."""
    assert map_status("nichts davon", rules.status_map) == (ShipmentStatus.UNKNOWN, None)


def test_first_match_nutzt_value_gruppe(rules: CarrierRules):
    """Die benannte Gruppe `value` schlaegt den Gesamttreffer."""
    assert first_match(rules.tracking_patterns, "Sendungsnummer: AC12345678") == "AC12345678"


def test_first_match_ohne_treffer(rules: CarrierRules):
    """Kein Muster passt — kein Wert."""
    assert first_match(rules.tracking_patterns, "keine Nummer") is None


def test_build_shipment_aus_regeln(rules: CarrierRules):
    """Aus Regeln und Mail entsteht eine vollstaendige Sendung."""
    shipment = build_shipment_from_rules(rules, _mail())

    assert shipment is not None
    assert shipment.id == "acme_ac12345678"
    assert shipment.carrier == "acme"
    assert shipment.tracking_number == "AC12345678"
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.status_text == "ist unterwegs"
    assert shipment.sender == "Kaffeeroesterei Nord"
    assert shipment.description == "Kaffeebohnen 1kg"
    assert shipment.destination_raw == "Musterstrasse 1, 12345 Musterstadt"
    assert shipment.tracking_url == "https://acme.example/AC12345678"
    assert shipment.delivered_at is None
    # 10-14 Uhr Berliner Zeit am 30.07.2026.
    assert shipment.eta_start == datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    assert shipment.eta_end == datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    assert [record.source_id for record in shipment.sources] == [SOURCE_IMAP]
    assert shipment.sources[0].reference == "<42@acme.com>"
    assert len(shipment.events) == 1
    assert shipment.events[0].status is ShipmentStatus.IN_TRANSIT


def test_build_shipment_setzt_delivered_at(rules: CarrierRules):
    """Bei Status `delivered` gilt ohne eigenes Datum der Zeitpunkt der Mail."""
    mail = _mail(subject="Ihre Sendung wurde zugestellt")
    shipment = build_shipment_from_rules(rules, mail)

    assert shipment is not None
    assert shipment.status is ShipmentStatus.DELIVERED
    assert shipment.delivered_at == MAIL_DATE


def test_build_shipment_ohne_nummer(rules: CarrierRules):
    """Ohne Tracking- und Bestellnummer gibt es keine Sendung."""
    mail = _mail(text="Wir informieren Sie, sobald es losgeht.")

    assert build_shipment_from_rules(rules, mail) is None


def test_build_shipment_nur_mit_bestellnummer(rules: CarrierRules):
    """Eine Bestellbestaetigung reicht fuer eine ID — die Versandmail rastet spaeter ein."""
    mail = _mail(
        subject="Bestellbestaetigung",
        text="Bestellnummer: ABC-12345\nIm Auftrag von Kaffeeroesterei Nord\n",
    )
    shipment = build_shipment_from_rules(rules, mail)

    assert shipment is not None
    assert shipment.tracking_number is None
    assert shipment.order_number == "ABC-12345"
    assert shipment.id.startswith("acme_order_")
    assert shipment.tracking_url is None


def test_build_shipments_liefert_jede_nummer(rules: CarrierRules):
    """Eine Sammelbestaetigung ergibt eine Sendung je Trackingnummer."""
    mail = _mail(
        text=(
            "Sendungsnummer: AC11111111\n"
            "Sendungsnummer: AC22222222\n"
            "Im Auftrag von Kaffeeroesterei Nord\n"
        )
    )

    shipments = build_shipments_from_rules(rules, mail)

    assert [shipment.tracking_number for shipment in shipments] == [
        "AC11111111",
        "AC22222222",
    ]
    assert len({shipment.id for shipment in shipments}) == 2
    # Der gemeinsame Kontext gilt fuer beide Sendungen.
    assert all(shipment.sender == "Kaffeeroesterei Nord" for shipment in shipments)


def test_build_shipments_ohne_nummer_ist_leer(rules: CarrierRules):
    """Ohne Tracking- und Bestellnummer gibt es nichts zuzuordnen."""
    assert build_shipments_from_rules(rules, _mail(text="Bald geht es los.")) == []


def test_rules_carrier_bindet_alles_zusammen(rules: CarrierRules):
    """Das Plugin selbst ist nur noch die Klammer um Regeln und Hilfen."""
    plugin = RulesCarrier(rules)

    assert plugin.carrier_id == "acme"
    assert plugin.name == "ACME Logistik"
    assert plugin.matches(_mail()) > 0
    assert plugin.tracking_url("AC12345678") == "https://acme.example/AC12345678"

    shipment = plugin.parse(_mail())
    assert shipment is not None
    assert shipment.id == "acme_ac12345678"
