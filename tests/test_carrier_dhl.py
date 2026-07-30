"""Tests fuer das Carrier-Plugin DHL / Deutsche Post.

Gearbeitet wird durchgaengig mit den anonymisierten `.eml`-Fixtures — jeder in
Step 1.3 abgedeckte Mailtyp hat hier genau einen Test, der die Felder prueft,
auf die es in der Oberflaeche ankommt.
"""

from collections.abc import Callable, Generator
from datetime import UTC, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.carriers.dhl import PLUGIN
from custom_components.delivery_tracker.mail import parse_mail
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus

Loader = Callable[[str], bytes]

BERLIN = ZoneInfo("Europe/Berlin")

PARCEL_URL = "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html?piececode="
LETTER_URL = "https://www.deutschepost.de/sendung/simpleQuery.html?form.sendungsnummer="

# Alle Mailtypen dieses Zustellers — die Grundlage der Rundum-Tests unten.
ALLE_MAILS = [
    "sendungsankuendigung.eml",
    "zustellzeitfenster.eml",
    "zustellung.eml",
    "nachbar.eml",
    "packstation.eml",
    "filiale.eml",
    "zustellversuch.eml",
    "wunschtag.eml",
    "retoure.eml",
    "einschreiben.eml",
    "express_kommt_heute.eml",
    "express_verspaetet.eml",
    "express_filiale.eml",
]


@pytest.fixture(autouse=True)
def berlin_timezone() -> Generator[None]:
    """Zustellzeitfenster sind lokal gemeint — hier durchgaengig Europe/Berlin."""
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(original)


def _shipments(email_fixture: Loader, name: str) -> list[Shipment]:
    """Alle Sendungen einer Fixture-Mail."""
    return PLUGIN.parse_all(parse_mail(email_fixture(f"dhl/{name}")))


def _one(email_fixture: Loader, name: str) -> Shipment:
    """Die eine Sendung einer Fixture-Mail."""
    shipments = _shipments(email_fixture, name)
    assert len(shipments) == 1
    return shipments[0]


def test_sendungsankuendigung(email_fixture: Loader):
    """Die Ankuendigung traegt noch keinen Scan, aber schon Nummer und Termin."""
    shipment = _one(email_fixture, "sendungsankuendigung.eml")

    assert shipment.id == "dhl_00340434161094042557"
    assert shipment.carrier == "dhl"
    assert shipment.tracking_number == "00340434161094042557"
    assert shipment.status is ShipmentStatus.LABEL_CREATED
    assert shipment.status_text == "Sendungsankündigung"
    assert shipment.sender == "Kaffeerösterei Nord"
    assert shipment.destination_raw == "Musterstraße 1, 12345 Musterstadt"
    assert shipment.tracking_url == f"{PARCEL_URL}00340434161094042557"
    # Ein Datum ohne Uhrzeit gilt als ganztaegig (lokale Tagesgrenzen).
    assert shipment.eta_start == datetime(2026, 7, 29, 22, 0, tzinfo=UTC)
    assert shipment.eta_end == datetime(2026, 7, 30, 22, 0, tzinfo=UTC)
    assert shipment.delivered_at is None


def test_zustellzeitfenster(email_fixture: Loader):
    """Zeitfenster-Mail: Die Nummer steht nur im Verfolgen-Link."""
    shipment = _one(email_fixture, "zustellzeitfenster.eml")

    assert shipment.tracking_number == "00340434161094042632"
    assert shipment.status is ShipmentStatus.OUT_FOR_DELIVERY
    assert shipment.sender == "Kaffeerösterei Nord"
    # „heute zwischen 14 und 18 Uhr", bezogen auf das Datum der Mail (29.07.).
    assert shipment.eta_start == datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    assert shipment.eta_end == datetime(2026, 7, 29, 16, 0, tzinfo=UTC)


def test_zustellung(email_fixture: Loader):
    """Die Zustellbestaetigung nennt den Zeitpunkt selbst."""
    shipment = _one(email_fixture, "zustellung.eml")

    assert shipment.status is ShipmentStatus.DELIVERED
    assert shipment.is_closed
    assert shipment.delivered_at == datetime(2026, 7, 30, 13, 42, tzinfo=UTC)


def test_ablage_beim_nachbarn(email_fixture: Loader):
    """Beim Nachbarn abgegeben ist zugestellt — der Nachbar ist der Abholort."""
    shipment = _one(email_fixture, "nachbar.eml")

    assert shipment.status is ShipmentStatus.DELIVERED
    assert shipment.pickup_location == "Familie Beispiel, Musterstraße 3, 12345 Musterstadt"
    assert shipment.pickup_code is None
    # Ohne eigene Zeitangabe gilt der Zeitpunkt der Mail.
    assert shipment.delivered_at == datetime(2026, 7, 30, 12, 20, tzinfo=UTC)


def test_packstation_trennt_abholort_und_abholcode(email_fixture: Loader):
    """Der Code ist ein Geheimnis und gehoert deshalb in ein eigenes Feld."""
    shipment = _one(email_fixture, "packstation.eml")

    assert shipment.status is ShipmentStatus.READY_FOR_PICKUP
    assert shipment.pickup_location == "Packstation 123, Musterstraße 5, 12345 Musterstadt"
    assert shipment.pickup_code == "483920"
    assert shipment.tracking_number == "00340434161094042632"


def test_filialzustellung(email_fixture: Loader):
    """Filial-Zustellung mit Identcode statt NVE."""
    shipment = _one(email_fixture, "filiale.eml")

    assert shipment.status is ShipmentStatus.READY_FOR_PICKUP
    assert shipment.pickup_location == "DHL Filiale 456, Musterplatz 2, 12345 Musterstadt"
    assert shipment.tracking_number == "560032240131"
    assert shipment.tracking_url == f"{PARCEL_URL}560032240131"


def test_zustellversuch(email_fixture: Loader):
    """Der gescheiterte Versuch nennt den naechsten Termin."""
    shipment = _one(email_fixture, "zustellversuch.eml")

    assert shipment.status is ShipmentStatus.DELIVERY_FAILED
    assert shipment.delivered_at is None
    assert shipment.eta_start == datetime(2026, 7, 30, 22, 0, tzinfo=UTC)
    assert shipment.eta_end == datetime(2026, 7, 31, 22, 0, tzinfo=UTC)


def test_wunschtag_aenderung(email_fixture: Loader):
    """Der geaenderte Wunschtag ueberschreibt den erwarteten Liefertermin."""
    shipment = _one(email_fixture, "wunschtag.eml")

    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.eta_start == datetime(2026, 7, 30, 22, 0, tzinfo=UTC)
    assert shipment.eta_end == datetime(2026, 7, 31, 22, 0, tzinfo=UTC)


def test_retouren_ankuendigung(email_fixture: Loader):
    """Eine Retoure verlaesst den Blick des Empfaengers — Status `returned`.

    Die Filiale in „geben Sie das Paket in einer Filiale ab" ist eine
    Abgabestelle, kein Abholort, und darf deshalb kein `pickup_location` werden.
    """
    shipment = _one(email_fixture, "retoure.eml")

    assert shipment.status is ShipmentStatus.RETURNED
    assert shipment.is_closed
    assert shipment.pickup_location is None


def test_einschreiben_nutzt_den_brief_regelsatz(email_fixture: Loader):
    """Deutsche Post: eigenes Nummernformat, eigene Sendungsverfolgung.

    Die Carrier-ID bleibt `dhl` — fuer den Nutzer ist es derselbe Zusteller.
    """
    mail = parse_mail(email_fixture("dhl/einschreiben.eml"))
    shipment = _one(email_fixture, "einschreiben.eml")

    assert PLUGIN.rules_for(mail) is PLUGIN.letter_rules
    assert shipment.carrier == "dhl"
    assert shipment.id == "dhl_rr123456789de"
    assert shipment.tracking_number == "RR123456789DE"
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.sender == "Amtsgericht Musterstadt"
    assert shipment.tracking_url == f"{LETTER_URL}RR123456789DE"
    assert shipment.eta_start == datetime(2026, 7, 29, 22, 0, tzinfo=UTC)


def test_paketmail_nutzt_den_paket_regelsatz(email_fixture: Loader):
    """Der Paket-Regelsatz ist der Normalfall und gewinnt bei Gleichstand."""
    mail = parse_mail(email_fixture("dhl/sendungsankuendigung.eml"))

    assert PLUGIN.rules_for(mail) is PLUGIN.rules


def test_express_kommt_heute(email_fixture: Loader):
    """Echtes DHL-Format: Nummer nur im Link, Text markdown-artig ausgezeichnet.

    „Ihre **Express Online Seller **Sendung wird Ihnen **heute** durch Ihre
    Paketzustellkraft zugestellt" — die Sternchen stehen mitten in der
    Formulierung, auf die die Regel ansetzt.
    """
    mail = parse_mail(email_fixture("dhl/express_kommt_heute.eml"))
    shipment = _one(email_fixture, "express_kommt_heute.eml")

    assert "**heute**" in mail.text
    assert shipment.tracking_number == "00340434161094042557"
    assert shipment.status is ShipmentStatus.OUT_FOR_DELIVERY
    assert shipment.eta_start == datetime(2026, 7, 29, 22, 0, tzinfo=UTC)
    assert shipment.eta_end == datetime(2026, 7, 30, 22, 0, tzinfo=UTC)


def test_express_verspaetet(email_fixture: Loader):
    """Der neue Zustelltag steht eine Zeile unter seiner Beschriftung."""
    shipment = _one(email_fixture, "express_verspaetet.eml")

    assert shipment.status is ShipmentStatus.DELAYED
    assert shipment.is_active
    # „Ihr neuer voraussichtlicher Zustelltag ist" / „Donnerstag, der 30.07."
    assert shipment.eta_start == datetime(2026, 7, 29, 22, 0, tzinfo=UTC)


def test_express_auf_dem_weg_zur_filiale(email_fixture: Loader):
    """Ohne konkreten Termin („in den naechsten 2 Werktagen") bleibt die ETA leer."""
    shipment = _one(email_fixture, "express_filiale.eml")

    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.eta_start is None


def test_sprachwahl_block_stoert_nicht(email_fixture: Loader):
    """Die Mails tragen vor dem Text 35 Zeilen Sprachumschalter mit Links."""
    mail = parse_mail(email_fixture("dhl/express_kommt_heute.eml"))

    assert "Previous group of languages" in mail.text
    assert PLUGIN.matches(mail) > 0


def test_newsletter_wird_nicht_angenommen(email_fixture: Loader):
    """Werbung desselben Absenders muss hart durchfallen."""
    mail = parse_mail(email_fixture("dhl/newsletter.eml"))

    assert PLUGIN.matches(mail) == 0
    assert PLUGIN.parse_all(mail) == []


@pytest.mark.parametrize("name", ALLE_MAILS)
def test_jede_mail_ergibt_eine_sendung_mit_dhl_id(email_fixture: Loader, name: str):
    """Kein abgedeckter Mailtyp darf ohne Sendung durchfallen."""
    shipments = _shipments(email_fixture, name)

    assert len(shipments) == 1
    assert shipments[0].id.startswith("dhl_")
    assert shipments[0].tracking_number


@pytest.mark.parametrize("name", ALLE_MAILS)
def test_tracking_urls_sind_aufrufbar_gebaut(email_fixture: Loader, name: str):
    """Eine URL, die kein Browser oeffnen kann, ist schlimmer als keine."""
    shipment = _shipments(email_fixture, name)[0]

    assert shipment.tracking_url is not None
    parts = urlsplit(shipment.tracking_url)
    assert parts.scheme == "https"
    assert parts.netloc
    assert shipment.tracking_number is not None
    assert shipment.tracking_number in shipment.tracking_url


@pytest.mark.parametrize(
    ("nummer", "erwartet"),
    [
        ("00340434161094042557", PARCEL_URL),
        ("560032240131", PARCEL_URL),
        ("RR123456789DE", LETTER_URL),
        ("rr123456789de", LETTER_URL),
    ],
)
def test_tracking_url_folgt_dem_nummernformat(nummer: str, erwartet: str):
    """Einschreiben-Nummern gehoeren in die Brief-, alles andere in die Paketverfolgung."""
    assert PLUGIN.tracking_url(nummer) == f"{erwartet}{nummer}"
