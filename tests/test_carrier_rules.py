"""Tests fuer das Schema und den Loader der `rules.yaml`."""

from pathlib import Path

import pytest
import yaml

from custom_components.delivery_tracker.carriers.rules import (
    EXTRACT_FIELDS,
    RULES_FILENAME,
    RulesError,
    load_rules,
    parse_rules,
)
from custom_components.delivery_tracker.models import ShipmentStatus
from custom_components.delivery_tracker.tracking import TrackingChecksum

MINIMAL = """
carrier: acme
name: ACME Logistik
"""

VOLLSTAENDIG = """
carrier: acme
name: ACME Logistik
score_bonus: -5
priority: 7
match:
  from_domains: ["@ACME.com", "acme.de"]
  subject_patterns: ["ihre sendung"]
  body_patterns: ["acme logistik gmbh"]
  newsletter_patterns: ["angebot der woche"]
tracking:
  patterns: ["\\\\b(AC\\\\d{8})\\\\b"]
  url: "https://acme.example/{tracking_number}"
  checksum: deutsche_post
  require_context: true
status_map:
  - status: delivered
    patterns: ["wurde zugestellt"]
  - status: in_transit
    patterns: ["ist unterwegs"]
dates:
  eta_patterns: ["voraussichtlich (?P<value>.+)"]
fields:
  sender: ["von (?P<value>.+)"]
"""


def _rules(text: str):
    """Regeln aus einem YAML-Text bauen."""
    return parse_rules(yaml.safe_load(text), origin="test.yaml")


def test_minimale_regeln_haben_leere_defaults():
    """Pflicht sind nur Carrier und Name; alles andere ist leer vorbelegt."""
    rules = _rules(MINIMAL)

    assert rules.carrier_id == "acme"
    assert rules.name == "ACME Logistik"
    assert rules.score_bonus == 0
    assert rules.priority == 0
    assert rules.from_domains == ()
    assert rules.newsletter_patterns == ()
    assert rules.status_map == ()
    # Ohne Angabe wird nur geprueft, was sich zweifelsfrei zuordnen laesst.
    assert rules.checksum is TrackingChecksum.AUTO
    assert rules.require_context is False
    assert rules.tracking_url("AC12345678") is None
    # Alle Extraktionsfelder existieren, auch wenn `fields` fehlt.
    assert set(rules.fields) == set(EXTRACT_FIELDS)
    assert rules.field_patterns("sender") == ()


def test_vollstaendige_regeln_werden_normalisiert():
    """Domains werden vereinheitlicht, Regex kompiliert, Status gemappt."""
    rules = _rules(VOLLSTAENDIG)

    assert rules.from_domains == ("acme.com", "acme.de")
    assert rules.score_bonus == -5
    assert rules.priority == 7
    assert rules.checksum is TrackingChecksum.DEUTSCHE_POST
    assert rules.require_context is True
    assert rules.newsletter_patterns[0].search("Unser ANGEBOT DER WOCHE")
    assert rules.status_map[0].status is ShipmentStatus.DELIVERED
    assert rules.status_map[1].status is ShipmentStatus.IN_TRANSIT
    assert rules.tracking_url("AC12345678") == "https://acme.example/AC12345678"
    # Regex sind vorkompiliert und ignorieren Gross-/Kleinschreibung.
    assert rules.tracking_patterns[0].search("Sendung AC12345678 unterwegs")
    assert rules.subject_patterns[0].search("IHRE SENDUNG ist da")


def test_unbekanntes_feld_wird_abgelehnt():
    """Ein Tippfehler im Regelwerk faellt sofort auf."""
    with pytest.raises(RulesError) as err:
        _rules("carrier: acme\nname: ACME\ntracking_url: https://x.example/{tracking_number}\n")

    assert "tracking_url" in str(err.value)
    assert "extra keys" in str(err.value)


def test_ungueltiger_regex_wird_abgelehnt():
    """Ein kaputtes Muster nennt Ausdruck und Grund."""
    with pytest.raises(RulesError) as err:
        _rules('carrier: acme\nname: ACME\ntracking:\n  patterns: ["(unbalanced"]\n')

    message = str(err.value)
    assert "ungueltiger regulaerer Ausdruck" in message
    assert "(unbalanced" in message


def test_unbekannter_status_wird_abgelehnt():
    """Nur die normalisierten Status sind erlaubt — mit Auflistung im Fehler."""
    with pytest.raises(RulesError) as err:
        _rules('carrier: acme\nname: ACME\nstatus_map:\n  - status: fliegt\n    patterns: ["x"]\n')

    message = str(err.value)
    assert "unbekannter Status" in message
    assert "out_for_delivery" in message


def test_unbekanntes_pruefverfahren_wird_abgelehnt():
    """Ein Tippfehler beim Verfahren wuerde sonst still alle Pruefungen abschalten."""
    with pytest.raises(RulesError) as err:
        _rules("carrier: acme\nname: ACME\ntracking:\n  checksum: luhn\n")

    message = str(err.value)
    assert "unbekanntes Pruefverfahren" in message
    assert "deutsche_post" in message


def test_tracking_url_ohne_platzhalter_wird_abgelehnt():
    """Eine URL-Vorlage ohne {tracking_number} waere nutzlos."""
    with pytest.raises(RulesError) as err:
        _rules('carrier: acme\nname: ACME\ntracking:\n  url: "https://acme.example/"\n')

    assert "{tracking_number}" in str(err.value)


def test_unbekannter_platzhalter_in_url_wird_abgelehnt():
    """Andere Platzhalter wuerden beim Formatieren zur Laufzeit knallen."""
    with pytest.raises(RulesError) as err:
        _rules('carrier: acme\nname: ACME\ntracking:\n  url: "https://x/{tracking_number}/{zip}"\n')

    assert "Platzhalter" in str(err.value)


@pytest.mark.parametrize(
    "carrier",
    ["ACME", "1acme", "acme-express", "acme express"],
)
def test_ungueltige_carrier_id_wird_abgelehnt(carrier: str):
    """Die Carrier-ID steckt in jeder Sendungs-ID und muss slug-tauglich sein."""
    with pytest.raises(RulesError):
        _rules(f"carrier: {carrier}\nname: ACME\n")


def test_fehlende_pflichtfelder_werden_abgelehnt():
    """Ohne Carrier und Name laesst sich nichts anfangen."""
    with pytest.raises(RulesError) as err:
        _rules("name: ACME\n")

    assert "carrier" in str(err.value)


def test_kein_mapping_wird_abgelehnt():
    """Eine Liste oder ein Skalar statt einer Zuordnung wird sauber gemeldet."""
    with pytest.raises(RulesError) as err:
        _rules("- eins\n- zwei\n")

    assert "YAML-Zuordnung" in str(err.value)


def test_fehlende_datei_wird_gemeldet():
    """Ein Plugin ohne `rules.yaml` scheitert mit Pfadangabe."""
    with pytest.raises(RulesError) as err:
        load_rules(Path("gibt/es/nicht") / RULES_FILENAME)

    assert RULES_FILENAME in str(err.value)


def test_beispiel_plugin_regeln_sind_gueltig():
    """Die mitgelieferte Vorlage muss immer laden — sie ist die Doku."""
    example = (
        Path(__file__).parent.parent
        / "custom_components"
        / "delivery_tracker"
        / "carriers"
        / "example"
    )
    rules = load_rules(example / RULES_FILENAME)

    assert rules.carrier_id == "example"
    assert rules.tracking_url("EX1234567890DE") == "https://track.example.com/de/EX1234567890DE"
