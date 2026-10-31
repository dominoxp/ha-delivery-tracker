"""Tests fuer das Carrier-Plugin AliExpress.

Die Fixtures sind aus echten Mails von 2026 abgeleitet (Artikel, Bestell- und
Trackingnummern erfunden). Sie bilden eine zusammenhaengende Kette: vier
Bestellungen, ein Zubringer `LP…`, das Sammelpaket `AP…` und dessen DHL-Paket
— dazu ein Hermes-Paket mit deutschen Mails.
"""

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from custom_components.delivery_tracker.carriers import CarrierRegistry, discover_plugins
from custom_components.delivery_tracker.carriers.aliexpress import PLUGIN, items_in, order_numbers
from custom_components.delivery_tracker.mail import mail_from_text, parse_mail
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus

Loader = Callable[[str], bytes]

CARRIERS = Path(__file__).parents[1] / "custom_components" / "delivery_tracker" / "carriers"
REGISTRY = CarrierRegistry(
    discover_plugins(CARRIERS, package="custom_components.delivery_tracker.carriers")
)

SENDER = "AliExpress <transaction@notice.aliexpress.com>"
BESTELLUNG_A = "8998771253088513"
BESTELLUNG_C = "8550430236738513"
BESTELLUNG_E = "8004918048988513"
SAMMELPAKET_BESTELLUNGEN = [BESTELLUNG_C, "8225620021708513", BESTELLUNG_A, "8179412833008513"]
DHL_PAKET = "00340434060877185651"
DHL_EINZEL = "00340434805571222802"
ZUBRINGER = "LP27760838749217"
SAMMELPAKET = "AP89226395272617"
HERMES_PAKET = "H1003689856591323458"


def _resolve(email_fixture: Loader, name: str) -> list[Shipment]:
    """Alle Sendungen einer Fixture-Mail ueber die echte Registry."""
    resolution = REGISTRY.resolve(parse_mail(email_fixture(f"aliexpress/{name}")))
    assert resolution.plugin is not None
    assert resolution.plugin.carrier_id == "aliexpress"
    return resolution.shipments


def _one(email_fixture: Loader, name: str) -> Shipment:
    """Die eine Sendung einer Fixture-Mail."""
    shipments = _resolve(email_fixture, name)
    assert len(shipments) == 1
    return shipments[0]


def test_bestellbestaetigung_ist_eine_bestellung_ohne_paket(email_fixture: Loader):
    """Bestellmails tragen keine Trackingnummer — Anker ist die Bestellnummer."""
    shipment = _one(email_fixture, "bestellung_bestaetigt.eml")

    assert shipment.id == f"aliexpress_order_aliexpress_{BESTELLUNG_A}"
    assert shipment.carrier == "aliexpress"
    assert shipment.tracking_number is None
    assert shipment.status is ShipmentStatus.ORDERED
    assert shipment.status_text == "Bestellauftrag bestätigt"
    assert shipment.sender == "AliExpress"
    assert shipment.order_numbers == [BESTELLUNG_A]
    assert shipment.items == ["Muster Rillenkugellager 5 x 1..."]
    assert shipment.destination_raw == "Musterstraße 1, 12345 Musterstadt"
    assert shipment.tracking_url == (
        f"https://www.aliexpress.com/p/order/detail.html?orderId={BESTELLUNG_A}"
    )


def test_versandte_bestellung_ist_unterwegs(email_fixture: Loader):
    """„Bestellung versandt" nennt noch keine Nummer, aber einen Stand."""
    shipment = _one(email_fixture, "bestellung_versandt.eml")

    assert shipment.id == f"aliexpress_order_aliexpress_{BESTELLUNG_C}"
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.items == ["Beispiel Linearfuehrung Schiene 30..."]


@pytest.mark.parametrize("name", ["bewertung.eml", "neuer_lieferstatus.eml", "werbung.eml"])
def test_mails_ohne_aussage_aendern_nichts(email_fixture: Loader, name: str):
    """Bewertung, Empfangsbestaetigung und Werbung: erkannt, aber keine Sendung.

    Die Bewertungsmail kommt per Zeitplan, auch wenn das Paket noch haengt —
    als „zugestellt" darf sie nicht zaehlen.
    """
    assert _resolve(email_fixture, name) == []


def test_sammelpaket_traegt_alle_bestellungen(email_fixture: Loader):
    """Das Sammelpaket nennt seine Bestellungen nur in den Verfolgen-Links."""
    shipment = _one(email_fixture, "package_ready_to_ship.eml")

    assert shipment.id == f"aliexpress_{SAMMELPAKET.lower()}"
    assert shipment.carrier == "aliexpress"
    assert shipment.status is ShipmentStatus.LABEL_CREATED
    assert shipment.status_text == "Ready to ship"
    assert shipment.order_numbers == SAMMELPAKET_BESTELLUNGEN
    assert shipment.order_number == BESTELLUNG_C
    assert len(shipment.items) == 3
    assert shipment.tracking_url == (
        f"https://global.cainiao.com/newDetail.htm?mailNoList={SAMMELPAKET}"
    )


def test_dhl_nummer_geht_an_dhl(email_fixture: Loader):
    """Meldet AliExpress eine DHL-Nummer, wird die Sendung eine DHL-Sendung.

    Dieselbe ID wie bei einer Mail von DHL selbst — beide landen in einer Zeile.
    """
    shipment = _one(email_fixture, "package_left_departure.eml")

    assert shipment.id == f"dhl_{DHL_PAKET}"
    assert shipment.carrier == "dhl"
    assert shipment.sender == "AliExpress"
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.status_text == "In global transit"
    assert shipment.order_numbers == SAMMELPAKET_BESTELLUNGEN
    assert "dhl.de" in (shipment.tracking_url or "")


def test_hermes_nummer_geht_an_hermes(email_fixture: Loader):
    """Die deutschen Paketmails: Hermes-Nummer ohne Pruefziffer, trotzdem eindeutig."""
    shipment = _one(email_fixture, "packstueck_mit_lokalem_kurier.eml")

    assert shipment.id == f"hermes_{HERMES_PAKET.lower()}"
    assert shipment.carrier == "hermes"
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.status_text == "Dem lokalen Kurier übergeben"
    assert len(shipment.order_numbers) == 3

    delivered = _one(email_fixture, "paket_zugestellt.eml")
    assert delivered.id == shipment.id
    assert delivered.status is ShipmentStatus.DELIVERED
    assert delivered.delivered_at is not None


@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("package_delivery_issue.eml", ShipmentStatus.DELIVERY_FAILED),
        ("package_ready_for_pickup.eml", ShipmentStatus.READY_FOR_PICKUP),
        ("package_delivered.eml", ShipmentStatus.DELIVERED),
    ],
)
def test_letzte_meile(email_fixture: Loader, name: str, status: ShipmentStatus):
    """Zustellproblem, Abholung und Zustellung des DHL-Pakets."""
    shipment = _one(email_fixture, name)

    assert shipment.id == f"dhl_{DHL_PAKET}"
    assert shipment.status is status


def test_paketmail_mit_zubringer_im_link_bleibt_beim_paket(email_fixture: Loader):
    """Die Zustellproblem-Mail verlinkt auch den Zubringer — Sendung ist das Paket im Betreff."""
    shipment = _one(email_fixture, "package_delivery_issue.eml")

    assert shipment.tracking_number == DHL_PAKET


def test_sammelmail_liefert_jedes_paket_mit_eigenem_stand(email_fixture: Loader):
    """„Your 2 packages…": welche Nummer zu welchem Block gehoert, steht nur im Link."""
    shipments = {
        shipment.tracking_number: shipment for shipment in _resolve(email_fixture, "sammelmail.eml")
    }

    assert set(shipments) == {ZUBRINGER, DHL_EINZEL}
    zubringer, einzel = shipments[ZUBRINGER], shipments[DHL_EINZEL]
    assert zubringer.carrier == "aliexpress"
    assert zubringer.status_text == "Cleared customs"
    assert zubringer.order_numbers == [BESTELLUNG_C]
    assert einzel.carrier == "dhl"
    assert einzel.status_text == "In global transit"
    assert einzel.order_numbers == [BESTELLUNG_E]
    assert all(shipment.status is ShipmentStatus.IN_TRANSIT for shipment in shipments.values())


def test_abholung_ohne_nummer_bleibt_bei_der_bestellung(email_fixture: Loader):
    """„Order …: collected by the carrier" nennt keine Paketnummer."""
    shipment = _one(email_fixture, "order_collected.eml")

    assert shipment.id == f"aliexpress_order_aliexpress_{BESTELLUNG_E}"
    assert shipment.status is ShipmentStatus.IN_TRANSIT


def test_geschlossene_bestellung_ist_ein_storno_signal(email_fixture: Loader):
    """„Your order … is closed" — entfernt wird im Coordinator, hier nur das Signal."""
    shipment = _one(email_fixture, "order_closed.eml")

    assert shipment.cancelled
    assert shipment.id == "aliexpress_order_aliexpress_8662829489698513"
    assert "cancelled" not in shipment.to_dict()


def test_ohne_hermes_plugin_bleibt_die_sendung_bei_aliexpress(email_fixture: Loader):
    """Ist Hermes abgeschaltet, fehlt es in der Registry — dann gibt es niemanden zum Uebergeben."""
    registry = CarrierRegistry(
        [plugin for plugin in REGISTRY.plugins if plugin.carrier_id != "hermes"]
    )
    mail = parse_mail(email_fixture("aliexpress/packstueck_mit_lokalem_kurier.eml"))

    (shipment,) = registry.resolve(mail).shipments

    assert shipment.carrier == "aliexpress"
    assert shipment.id == f"aliexpress_{HERMES_PAKET.lower()}"


# Betreffs aus echten Mails, Nummern erfunden. Die Statuszeile darunter
# entscheidet bei einigen Mails nichts — massgeblich ist der Betreff.
@pytest.mark.parametrize(
    ("subject", "status"),
    [
        ("Package LP27760838749217: collected by the carrier", ShipmentStatus.IN_TRANSIT),
        (f"Package {SAMMELPAKET} has an update", ShipmentStatus.IN_TRANSIT),
        (f"Package {DHL_PAKET}: in your country/region", ShipmentStatus.IN_TRANSIT),
        (f"Package {DHL_PAKET} has cleared customs", ShipmentStatus.IN_TRANSIT),
        (f"Package {DHL_PAKET}: with local carrier", ShipmentStatus.IN_TRANSIT),
        (f"Package {HERMES_PAKET}: at carrier facility", ShipmentStatus.IN_TRANSIT),
        (f"Package {DHL_PAKET} is out for delivery", ShipmentStatus.OUT_FOR_DELIVERY),
        (f"Package {HERMES_PAKET}: out for redelivery", ShipmentStatus.OUT_FOR_DELIVERY),
        (f"Packstück {HERMES_PAKET} hat die Abflugregion verlassen", ShipmentStatus.IN_TRANSIT),
        (f"Packstück {HERMES_PAKET} hat eine Aktualisierung", ShipmentStatus.IN_TRANSIT),
        (f"Packstück {HERMES_PAKET}: In Ihrem Land / Ihrer Region", ShipmentStatus.IN_TRANSIT),
        (f"Packstück {HERMES_PAKET}: lokal unterwegs", ShipmentStatus.IN_TRANSIT),
        (f"Zollabfertigung für {HERMES_PAKET} wurde beendet", ShipmentStatus.IN_TRANSIT),
        (f"Packstück {HERMES_PAKET}: Neuzustellung", ShipmentStatus.OUT_FOR_DELIVERY),
        (f"Paket {HERMES_PAKET} wird zugestellt", ShipmentStatus.OUT_FOR_DELIVERY),
    ],
)
def test_status_aus_dem_betreff(subject: str, status: ShipmentStatus):
    """Alle Paketmail-Betreffs aus dem Postfach, englisch wie deutsch."""
    mail = mail_from_text(subject, SENDER, "<html><body><p>Hallo Max user,</p></body></html>")

    assert PLUGIN.matches(mail) > 0
    (shipment,) = PLUGIN.parse_all(mail)
    assert shipment.status is status


@pytest.mark.parametrize(
    "subject",
    [
        f"Bestellung {BESTELLUNG_A}: auf Bestätigung wird gewartet",
        f"Bestellung {BESTELLUNG_A}: Wie war Ihr Einkaufserlebnis?",
        f"Bestellung {BESTELLUNG_A}: Neuer Lieferstatus",
        "Ihr Verspätete-Lieferung-Coupon-Code",
    ],
)
def test_timer_und_infomails_ergeben_keine_sendung(subject: str):
    """Ohne Statusaussage im Betreff keine Sendung — auch nicht als „bestellt"."""
    mail = mail_from_text(subject, SENDER, f"<p>Ihre Bestellung {BESTELLUNG_A} aliexpress.com</p>")

    assert PLUGIN.parse_all(mail) == []


def test_artikelblock_titel_ohne_varianten():
    """Titel, Variante, Menge — und Varianten ohne eigenen Titel fallen weg."""
    lines = [
        "Testartikel Sensor...",
        "60cm",
        "x4",
        "Testartikel Kabel...",
        "Variante B x2",
        "Muster LED-Streifen ...",
        "Schwarz, 5m",
        "x1",
        "Weiss, 5m",
        "Schwarz, 10m",
    ]

    assert items_in(lines) == [
        "Testartikel Sensor...",
        "Testartikel Kabel...",
        "Muster LED-Streifen ...",
    ]


def test_offene_links_halten_die_event_loop_nicht_auf():
    """Unverschlossene Verfolgen-Links laufen nicht je bis ans Mailende."""
    html = '<a href="/p/tracking/x">' * 8000

    start = time.perf_counter()
    assert order_numbers(html) == []
    assert time.perf_counter() - start < 1


def test_linktext_endet_am_naechsten_link():
    """Ein offener Link verschluckt den folgenden nicht."""
    html = (
        '<a href="/p/tracking/?tradeOrderId=111111111111">kaputt'
        f'<a class="b" href="/p/tracking/?tradeOrderId={BESTELLUNG_A}"><span>Track</span></A >'
    )

    assert order_numbers(html) == [BESTELLUNG_A]
