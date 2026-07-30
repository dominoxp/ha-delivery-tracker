"""Tests fuer das Carrier-Plugin Amazon.

Die Fixtures sind aus echten Mails von 2026 abgeleitet — mit allem, was daran
unbequem ist: dem Fortschrittsbalken „Bestellt / Versendet / In Zustellung /
Zugestellt", der in *jeder* Mail steht, Terminen ohne Jahreszahl („Zustellung:
Montag") und dem Umstand, dass es gar keine Trackingnummer gibt.
"""

from collections.abc import Callable, Generator
from datetime import UTC, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.carriers.amazon import PLUGIN, packages, product_description
from custom_components.delivery_tracker.mail import MailMessage, parse_mail
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus

Loader = Callable[[str], bytes]

BERLIN = ZoneInfo("Europe/Berlin")
BESTELLNUMMER = "303-1234567-1234567"
LOCKER = (
    "Amazon Locker - Musterblume, MUSTERSHOP - Musterstadt - "
    "Musterstraße 1, Musterstraße 1, Musterstadt, 12345"
)
TRACKER = "https://www.amazon.de/progress-tracker/package"

# Alle Mailtypen, aus denen eine Sendung entstehen muss.
ALLE_MAILS = [
    "bestellbestaetigung.eml",
    "versandbestaetigung.eml",
    "versand_sammelpaket.eml",
    "locker_bereit.eml",
    "locker_sammelpaket.eml",
    "locker_abgeholt.eml",
    "logistics_zeitfenster.eml",
    "verspaetet.eml",
    "zugestellt.eml",
]


@pytest.fixture(autouse=True)
def berlin_timezone() -> Generator[None]:
    """Liefertermine sind lokal gemeint — hier durchgaengig Europe/Berlin."""
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(original)


def _shipments(email_fixture: Loader, name: str) -> list[Shipment]:
    """Alle Sendungen einer Fixture-Mail."""
    return PLUGIN.parse_all(parse_mail(email_fixture(f"amazon/{name}")))


def _one(email_fixture: Loader, name: str) -> Shipment:
    """Die eine Sendung einer Fixture-Mail."""
    shipments = _shipments(email_fixture, name)
    assert len(shipments) == 1
    return shipments[0]


def test_bestellbestaetigung_ergibt_eine_sendung_je_paket(email_fixture: Loader):
    """Vier Artikel mit vier Terminen sind vier Sendungen, nicht eine.

    Ohne Trackingnummer haetten alle dieselbe ID; erst der Paket-Zusatz haelt
    sie auseinander.
    """
    shipments = _shipments(email_fixture, "bestellbestaetigung.eml")

    assert len(shipments) == 2
    assert [shipment.id for shipment in shipments] == [
        f"amazon_order_303_1234567_1234567_{index}" for index in (0, 1)
    ]
    assert all(shipment.status is ShipmentStatus.ORDERED for shipment in shipments)
    assert all(shipment.order_number == BESTELLNUMMER for shipment in shipments)
    assert all(shipment.tracking_number is None for shipment in shipments)
    # Jedes Paket traegt seinen eigenen Artikel und seinen eigenen Termin.
    assert [shipment.description for shipment in shipments] == [
        "Kabelloses Ladegerät 15 W, schwarz",
        "USB-C Kabel 2 m, geflochten",
    ]
    # „Zustellung: Montag" — Wochentag ohne Datum, bezogen auf den 25.07. (Sa).
    assert shipments[0].eta_start == datetime(2026, 7, 26, 22, 0, tzinfo=UTC)
    # „Ankunft morgen".
    assert shipments[1].eta_start == datetime(2026, 7, 25, 22, 0, tzinfo=UTC)


def test_fortschrittsbalken_faelscht_den_status_nicht(email_fixture: Loader):
    """„In Zustellung" steht als Layoutzeile in jeder Mail — auch in dieser.

    Ein Muster, das darauf anspringt, macht aus einer Bestellbestaetigung eine
    Sendung in Zustellung. Genau das darf nicht passieren.
    """
    mail = parse_mail(email_fixture("amazon/bestellbestaetigung.eml"))

    assert "In Zustellung" in mail.text
    assert _shipments(email_fixture, "bestellbestaetigung.eml")[0].status is ShipmentStatus.ORDERED


def test_versandbestaetigung_nutzt_die_shipment_id(email_fixture: Loader):
    """Amazon vergibt keine Trackingnummer — die `shipmentId` tritt an ihre Stelle."""
    shipment = _one(email_fixture, "versandbestaetigung.eml")

    assert shipment.id == "amazon_order_303_1234567_1234567_sh4tpkg01"
    assert shipment.tracking_number is None
    assert shipment.order_number == BESTELLNUMMER
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.description == "Kabelloses Ladegerät 15 W, schwarz"
    assert shipment.destination_raw == LOCKER
    assert shipment.eta_start == datetime(2026, 7, 26, 22, 0, tzinfo=UTC)
    # Der Verfolgen-Link der Mail, ohne die Klick-Zaehlparameter.
    assert shipment.tracking_url is not None
    assert shipment.tracking_url.startswith(TRACKER)
    assert "shipmentId=Sh4tPkG01" in shipment.tracking_url
    assert "ref_=" not in shipment.tracking_url
    assert "vt=" not in shipment.tracking_url


def test_versand_und_abholung_sind_dieselbe_sendung(email_fixture: Loader):
    """Die `shipmentId` verbindet Versand- und Zustellmail zu einer Sendung."""
    versand = _one(email_fixture, "versandbestaetigung.eml")
    abgeholt = _one(email_fixture, "locker_abgeholt.eml")

    assert versand.id == abgeholt.id
    assert abgeholt.status is ShipmentStatus.DELIVERED
    assert abgeholt.is_closed
    # „Heute abgeholt" traegt keine Uhrzeit — dann gilt der Zeitpunkt der Mail.
    assert abgeholt.delivered_at == datetime(2026, 7, 30, 16, 43, tzinfo=UTC)


def test_sammelpaket_nennt_den_ersten_artikel_und_zaehlt_den_rest(email_fixture: Loader):
    """Zwei Artikel in einem Paket ergeben eine Sendung mit einer Beschreibung."""
    shipment = _one(email_fixture, "versand_sammelpaket.eml")

    assert shipment.id == "amazon_order_303_1234567_1234567_sh4tpkg02"
    assert shipment.description == "USB-C Kabel 2 m, geflochten (+1 weiterer Artikel)"
    # Der Anzeigetext kuerzt, die Artikelliste nicht.
    assert shipment.items == ["USB-C Kabel 2 m, geflochten", "Schutzhülle transparent"]


def test_artikelliste_ueberlebt_die_wechselnde_reihenfolge(email_fixture: Loader):
    """Amazon zaehlt dieselben Artikel je Mail anders auf.

    Deshalb reicht `description` als Bindeglied nicht — die beiden Sendungen
    haetten verschiedene Anzeigetexte. Erst die vollstaendige Artikelmenge macht
    sie in Step 3.2 vergleichbar.
    """
    versand = _one(email_fixture, "versand_sammelpaket.eml")
    locker = _one(email_fixture, "locker_sammelpaket.eml")

    assert versand.description != locker.description
    assert versand.items != locker.items
    assert set(versand.items) == set(locker.items)
    assert versand.order_number == locker.order_number


def test_amazon_locker_trennt_abholort_und_abholcode(email_fixture: Loader):
    """Der Abholcode steht nur im Ziel des „Abholung starten"-Links."""
    shipment = _one(email_fixture, "locker_bereit.eml")

    assert shipment.status is ShipmentStatus.READY_FOR_PICKUP
    assert shipment.pickup_location == LOCKER
    assert shipment.pickup_code == "ab12cd"
    assert shipment.order_number == BESTELLNUMMER
    # „Abholung am 30. Juli".
    assert shipment.eta_start == datetime(2026, 7, 29, 22, 0, tzinfo=UTC)
    assert shipment.eta_end == datetime(2026, 7, 30, 22, 0, tzinfo=UTC)


def test_locker_mails_bekommen_eine_eigene_id(email_fixture: Loader):
    """Die Abholmails nennen `pkgId` statt `shipmentId` — eine andere Kennung.

    Sie fuehren deshalb vorerst eine eigene Sendung. Das Zusammenfuehren ueber
    die Bestellnummer ist Aufgabe von Step 3.2.
    """
    bereit = _one(email_fixture, "locker_bereit.eml")
    versand = _one(email_fixture, "versandbestaetigung.eml")

    assert bereit.id != versand.id
    assert bereit.order_number == versand.order_number


def test_lieferung_verspaetet(email_fixture: Loader):
    """Die Verspaetung ist ein Rueckschritt und traegt den neuen Termin."""
    shipment = _one(email_fixture, "verspaetet.eml")

    assert shipment.status is ShipmentStatus.DELAYED
    assert shipment.is_active
    # „Zustellung: Freitag", bezogen auf Montag den 27.07.
    assert shipment.eta_start == datetime(2026, 7, 30, 22, 0, tzinfo=UTC)


def test_zugestellt(email_fixture: Loader):
    """Eine zugestellte Sendung bekommt keinen Liefertermin mehr."""
    shipment = _one(email_fixture, "zugestellt.eml")

    assert shipment.status is ShipmentStatus.DELIVERED
    assert shipment.delivered_at == datetime(2026, 7, 28, 13, 2, tzinfo=UTC)
    assert shipment.eta_start is None
    assert shipment.eta_end is None


def test_trackingnummer_schlaegt_die_paket_kennung(email_fixture: Loader):
    """Steht ausnahmsweise doch eine `TBA`-Nummer drin, ist sie der Anker."""
    shipment = _one(email_fixture, "logistics_zeitfenster.eml")

    assert shipment.id == "amazon_tba123456789012"
    assert shipment.tracking_number == "TBA123456789012"
    assert shipment.status is ShipmentStatus.OUT_FOR_DELIVERY
    # „Zustellung: heute zwischen 14 und 18 Uhr" am 28.07.
    assert shipment.eta_start == datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert shipment.eta_end == datetime(2026, 7, 28, 16, 0, tzinfo=UTC)


def test_werbemail_wird_nicht_angenommen(email_fixture: Loader):
    """Werbung von amazon.de ist der haeufigste Fall im Postfach."""
    mail = parse_mail(email_fixture("amazon/werbung.eml"))

    assert PLUGIN.matches(mail) == 0
    assert PLUGIN.parse_all(mail) == []


def test_linktexte_eroeffnen_keinen_paketblock(email_fixture: Loader):
    """„Lieferung verfolgen" sieht wie eine Terminzeile aus, ist aber keine."""
    mail = parse_mail(email_fixture("amazon/versandbestaetigung.eml"))

    assert "Lieferung verfolgen" in mail.text
    assert len(packages(mail.searchable_text)) == 1


def test_produktbeschreibung_ohne_artikelzeile():
    """Ohne Artikel bleibt das Feld leer statt falsch gefuellt."""
    mail = MailMessage(
        message_id="<x@amazon.de>",
        subject="Deine Bestellung",
        text="Bestellnr.\n303-1234567-1234567\nVielen Dank.\n",
    )

    assert product_description(mail) is None


def test_produktbeschreibung_ueberspringt_den_werbeblock():
    """Unter der Bestellung stehen weitere Produktlinks — die zaehlen nicht."""
    mail = MailMessage(
        message_id="<x@amazon.de>",
        subject="Deine Bestellung",
        text=(
            "Zustellung: Montag\n"
            "* Kabelloses Ladegerät 15 W, schwarz\n"
            "  Menge: 1\n"
            "Empfehlungen für dich\n"
            "* Saugroboter mit Wischfunktion\n"
        ),
    )

    assert product_description(mail) == "Kabelloses Ladegerät 15 W, schwarz"


@pytest.mark.parametrize("name", ALLE_MAILS)
def test_jede_mail_ergibt_mindestens_eine_sendung(email_fixture: Loader, name: str):
    """Kein abgedeckter Mailtyp darf ohne Sendung durchfallen."""
    shipments = _shipments(email_fixture, name)

    assert shipments
    for shipment in shipments:
        assert shipment.carrier == "amazon"
        assert shipment.id.startswith("amazon_")
        # Die Bestellnummer ist das Bindeglied fuer Step 3.2 und fehlt nie.
        assert shipment.order_number == BESTELLNUMMER


@pytest.mark.parametrize("name", ALLE_MAILS)
def test_tracking_urls_sind_aufrufbar_gebaut(email_fixture: Loader, name: str):
    """Eine URL, die kein Browser oeffnen kann, ist schlimmer als keine."""
    for shipment in _shipments(email_fixture, name):
        if shipment.tracking_url is None:
            continue
        parts = urlsplit(shipment.tracking_url)
        assert parts.scheme == "https"
        assert parts.netloc.endswith("amazon.de")
        assert parts.query
