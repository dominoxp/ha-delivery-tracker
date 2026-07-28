"""Tests fuer Normalisierung, Pruefziffern und das Auffinden von Trackingnummern.

Die UPS- und NVE-Beispiele unten sind echte Nummernformate mit korrekter
Pruefziffer; die Leit- und Identcodes sind nach der veroeffentlichten
Gewichtung 4/9 gebildet. Die „kaputten" Varianten unterscheiden sich jeweils in
genau einer Stelle — genau der Fall, den ein Tippfehler oder ein zu weites
Regex-Muster erzeugt.
"""

import re

import pytest

from custom_components.delivery_tracker.tracking import (
    ChecksumResult,
    TrackingChecksum,
    checksum_result,
    context_score,
    find_tracking_numbers,
    iter_url_values,
    looks_like_tracking_number,
    normalize_tracking_number,
)

# Gueltige Nummern.
UPS_OK = "1Z999AA10123456784"
UPS_OK_2 = "1Z12345E1512345676"
NVE_OK = "00340434161094015902"
LEITCODE_OK = "12345678901236"
IDENTCODE_OK = "563322110042"

# Dieselben Nummern mit genau einer verfaelschten Stelle.
UPS_KAPUTT = "1Z999AA10123456785"
UPS_KAPUTT_2 = "1Z999AA10123456794"
NVE_KAPUTT = "00340434161094015903"
LEITCODE_KAPUTT = "12345678901237"
IDENTCODE_KAPUTT = "563322110043"

EX_NUMMER = re.compile(r"\b(EX\d{10}DE)\b", re.IGNORECASE)
# Bewusst weit — so sieht der generische Fallback aus Step 1.4 aus.
GENERISCH = re.compile(r"\b([A-Z]{0,3}\d[A-Z0-9]{7,19})\b", re.IGNORECASE)


# ----------------------------------------------------------------------
# Normalisierung
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("JJD 0099-8899", "JJD00998899"),
        ("jjd00998899", "JJD00998899"),
        ("1Z 999AA1 0123 4567 84", UPS_OK),
        ("  EX1234567890DE.  ", "EX1234567890DE"),
        ("003-4043_4161", "00340434161"),
        # Typografische Striche, wie HTML-Mails sie gern einsetzen: geschuetzter
        # Bindestrich (U+2011) und Halbgeviertstrich (U+2013).
        ("003‑4043–4161", "00340434161"),  # noqa: RUF001
        ("", ""),
    ],
)
def test_normalisierung(eingabe: str, erwartet: str):
    """Schreibweise und Trennzeichen duerfen dieselbe Sendung nicht spalten."""
    assert normalize_tracking_number(eingabe) == erwartet


# ----------------------------------------------------------------------
# Pruefziffern
# ----------------------------------------------------------------------


@pytest.mark.parametrize("nummer", [UPS_OK, UPS_OK_2])
def test_ups_pruefziffer_akzeptiert_gueltige_nummern(nummer: str):
    """Eine gueltige UPS-Nummer muss erkannt werden — sonst geht sie verloren."""
    assert checksum_result(nummer) is ChecksumResult.VALID
    assert checksum_result(nummer, TrackingChecksum.UPS) is ChecksumResult.VALID


@pytest.mark.parametrize("nummer", [UPS_KAPUTT, UPS_KAPUTT_2])
def test_ups_pruefziffer_lehnt_verfaelschte_nummern_ab(nummer: str):
    """Eine einzige falsche Stelle muss auffallen."""
    assert checksum_result(nummer) is ChecksumResult.INVALID


def test_dhl_nve_pruefziffer():
    """Die 20-stellige DHL-Sendungsnummer ist eine NVE mit GS1-Pruefziffer."""
    assert checksum_result(NVE_OK) is ChecksumResult.VALID
    assert checksum_result(NVE_KAPUTT) is ChecksumResult.INVALID


def test_deutsche_post_leitcode_und_identcode():
    """Leitcode (14) und Identcode (12) nutzen die Gewichtung 4/9."""
    scheme = TrackingChecksum.DEUTSCHE_POST

    assert checksum_result(LEITCODE_OK, scheme) is ChecksumResult.VALID
    assert checksum_result(LEITCODE_KAPUTT, scheme) is ChecksumResult.INVALID
    assert checksum_result(IDENTCODE_OK, scheme) is ChecksumResult.VALID
    assert checksum_result(IDENTCODE_KAPUTT, scheme) is ChecksumResult.INVALID


def test_auto_prueft_mehrdeutige_formate_nicht():
    """Zwoelfstellige Ziffernfolgen nutzen mehrere Zusteller mit eigenen Verfahren.

    Wuerde `auto` hier die Deutsche-Post-Regel anlegen, verwuerfe die Integration
    gueltige DPD- und GLS-Nummern.
    """
    assert checksum_result(LEITCODE_KAPUTT) is ChecksumResult.UNKNOWN
    assert checksum_result(IDENTCODE_KAPUTT) is ChecksumResult.UNKNOWN


def test_ohne_verfahren_wird_nicht_geprueft():
    """`none` schaltet die Pruefung ab — fuer Zusteller ganz ohne Pruefziffer."""
    assert checksum_result(UPS_KAPUTT, TrackingChecksum.NONE) is ChecksumResult.UNKNOWN


def test_unbekanntes_format_bleibt_unbewertet():
    """Kein Verfahren zu kennen ist kein Ablehnungsgrund."""
    assert checksum_result("EX1234567890DE") is ChecksumResult.UNKNOWN
    assert checksum_result("") is ChecksumResult.UNKNOWN


# ----------------------------------------------------------------------
# Kontext
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("Sendungsnummer: X", 1),
        ("Ihre Trackingnummer lautet X", 1),
        ("Bestellnummer: X", -1),
        ("Rechnungsnummer X", -1),
        ("Bestellnummer 4711, Sendungsnummer: X", 1),
        ("Sendungsnummer 4711, Rechnungsnummer: X", -1),
        ("Guten Tag, X", 0),
    ],
)
def test_context_score(text: str, erwartet: int):
    """Es zaehlt das naechstgelegene Schluesselwort, nicht das erste im Satz."""
    assert context_score(text, text.index("X")) == erwartet


# ----------------------------------------------------------------------
# Fundstellen
# ----------------------------------------------------------------------


def test_bestellnummer_wird_nicht_als_trackingnummer_erkannt():
    """Der wichtigste Fehltreffer ueberhaupt — Bestellnummern sehen gleich aus."""
    text = "Bestellnummer: 302123456712345\nSendungsnummer: EX1234567890DE"

    gefunden = find_tracking_numbers(text, [GENERISCH])

    assert "EX1234567890DE" in gefunden
    assert "302123456712345" not in gefunden


def test_require_context_verlangt_ein_ankuendigendes_wort():
    """Weite Muster fangen sonst jede laengere Zeichenfolge ein."""
    text = "Bestellnummer: 302123456712345\nSendungsnummer: EX1234567890DE"

    assert find_tracking_numbers(text, [GENERISCH], require_context=True) == ["EX1234567890DE"]


def test_gueltige_pruefziffer_schlaegt_schlechten_kontext():
    """Was die Pruefziffer belegt, kippt kein missverstaendliches Umfeld."""
    text = f"Rechnungsnummer: {UPS_OK}"

    assert find_tracking_numbers(text, [GENERISCH]) == [UPS_OK]


def test_falsche_pruefziffer_verwirft_trotz_gutem_kontext():
    """Die Pruefziffer ist das harte Kriterium."""
    text = f"Sendungsnummer: {UPS_KAPUTT}"

    assert find_tracking_numbers(text, [GENERISCH]) == []


def test_nummer_aus_url_parameter():
    """Bei vielen Zustellern steht die Nummer nur im Ziel des Verfolgen-Buttons."""
    text = "Verfolgen Sie Ihr Paket: https://track.example.com/de?idc=EX1234567890DE"

    assert find_tracking_numbers(text, [EX_NUMMER], require_context=True) == ["EX1234567890DE"]


def test_nummer_aus_url_pfad():
    """Manche Zusteller haengen die Nummer direkt an den Pfad."""
    text = "Sendung verfolgen (https://track.example.com/de/EX9988776655DE)"

    assert find_tracking_numbers(text, [EX_NUMMER]) == ["EX9988776655DE"]


def test_iter_url_values_liefert_parameter_und_pfad():
    """Beides ist als Fundstelle interessant, aber nur der Parameter zaehlt als Kontext."""
    text = "https://track.example.com/de/EX1DE?trackingNumber=EX2DE&lang=de"

    werte = dict(iter_url_values(text))

    assert werte["EX2DE"] is True
    assert werte["EX1DE"] is False
    assert werte["de"] is False


def test_mehrere_nummern_in_dokumentreihenfolge():
    """Eine Sammelbestaetigung liefert alle Nummern, ohne Dubletten."""
    text = (
        "Sendungsnummer: EX1111111111DE\n"
        "Sendungsnummer: EX2222222222DE\n"
        "Sendungsnummer: EX1111111111DE\n"
    )

    assert find_tracking_numbers(text, [EX_NUMMER]) == [
        "EX1111111111DE",
        "EX2222222222DE",
    ]


def test_ohne_muster_oder_text_gibt_es_nichts():
    """Beides sind gueltige Zustaende, keine Fehler."""
    assert find_tracking_numbers("", [EX_NUMMER]) == []
    assert find_tracking_numbers("Sendungsnummer: EX1234567890DE", []) == []


def test_obergrenze_greift():
    """Ein zu weites Muster darf den Store nicht fluten."""
    text = "\n".join(f"Sendungsnummer: EX{index:010d}DE" for index in range(30))

    assert len(find_tracking_numbers(text, [EX_NUMMER], limit=5)) == 5


@pytest.mark.parametrize(
    ("wert", "erwartet"),
    [
        ("EX1234567890DE", True),
        (UPS_OK, True),
        ("kurz1", False),
        ("NURBUCHSTABEN", False),
        ("EX-1234-567890-DE", True),
        ("1" * 40, False),
    ],
)
def test_looks_like_tracking_number(wert: str, erwartet: bool):
    """Grobe Formpruefung fuer den generischen Fallback."""
    assert looks_like_tracking_number(wert) is erwartet
