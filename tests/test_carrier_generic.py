"""Tests fuer den generischen Fallback.

Er ist der gefaehrlichste Teil der Erkennung: Jede Zahl, die er faelschlich
annimmt, steht als Geistersendung im Dashboard. Die Negativtests sind deshalb
mindestens so wichtig wie die Positivtests.
"""

from collections.abc import Callable

import pytest

from custom_components.delivery_tracker.carriers.generic import PLUGIN, carrier_for
from custom_components.delivery_tracker.mail import MailMessage, parse_mail
from custom_components.delivery_tracker.models import ShipmentStatus

Loader = Callable[[str], bytes]


def _mail(text: str, *, from_address: str = "versand@shop.example") -> MailMessage:
    """Eine Shop-Mail mit dem gegebenen Text."""
    return MailMessage(
        message_id="<generic@shop.example>",
        subject="Versandinformation",
        text=text,
        from_address=from_address,
        from_name="Beispiel-Shop",
    )


def test_unbekannter_zusteller_mit_gueltiger_nummer(email_fixture: Loader):
    """Kein Plugin fuer den Zusteller — die Nummer wird trotzdem erfasst."""
    shipments = PLUGIN.parse_all(parse_mail(email_fixture("generic/unbekannter_zusteller.eml")))

    assert len(shipments) == 1
    shipment = shipments[0]
    assert shipment.id == "generic_cx112233447cn"
    assert shipment.carrier == "generic"
    assert shipment.tracking_number == "CX112233447CN"
    assert shipment.status is ShipmentStatus.UNKNOWN
    assert shipment.events == []
    # Schreibt kein Zusteller, ist der Absender der Haendler.
    assert shipment.sender == "Kaffeerösterei Nord"
    assert shipment.tracking_url == "https://t.17track.net/de#nums=CX112233447CN"


def test_rechnung_ergibt_keine_sendung(email_fixture: Loader):
    """Gueltige Pruefziffern hinter „Rechnungsnummer" und „Kundennummer" zaehlen nicht."""
    mail = parse_mail(email_fixture("generic/rechnung.eml"))

    assert PLUGIN.matches(mail) == 0
    assert PLUGIN.parse_all(mail) == []


def test_shop_mail_ohne_trackingnummer_ergibt_nichts(email_fixture: Loader):
    """Eine Bestellnummer allein ist keine Sendung — ueber keine API verfolgbar."""
    mail = parse_mail(email_fixture("generic/shop_ohne_nummer.eml"))

    assert PLUGIN.matches(mail) == 0
    assert PLUGIN.parse_all(mail) == []


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        # UPS: am Praefix eindeutig, die Pruefziffer reicht.
        ("Ihre Sendung: 1Z0E12345678901235", ["1Z0E12345678901235"]),
        # S10: Buchstabenrahmen plus Pruefziffer.
        ("Trackingnummer LX876543216NL", ["LX876543216NL"]),
        # NVE mit Ankuendigung.
        ("Sendungsnummer: 00340434161094042809", ["00340434161094042809"]),
        # NVE im Tracking-Parameter eines Links.
        ("https://track.example/?trackingnumber=00340434161094042809", ["00340434161094042809"]),
        # NVE ohne Ankuendigung — jede zehnte Ziffernfolge hat zufaellig eine gueltige.
        ("Referenz 00340434161094042809", []),
        # Falsche Pruefziffern.
        ("Sendungsnummer: 1Z0E12345678901236", []),
        ("Trackingnummer: LX876543217NL", []),
        ("Sendungsnummer: 00340434161094042810", []),
        # Gueltig, aber ausdruecklich etwas anderes.
        ("Rechnungsnummer: 1Z0E12345678901235", []),
        ("Kundennummer: 00340434161094042809", []),
        # Formate ohne Pruefverfahren liefert der Fallback nie.
        ("Sendungsnummer: 09981122334455", []),
        ("Sendungsnummer: JJD000390012345678", []),
    ],
)
def test_nummernauswahl(text: str, erwartet: list[str]):
    """Nur Nummern mit stimmender Pruefziffer und ohne Gegenanzeige kommen durch."""
    assert PLUGIN.tracking_numbers(_mail(text)) == erwartet


@pytest.mark.parametrize(
    ("nummer", "domain", "erwartet"),
    [
        ("1Z0E12345678901235", "shop.example", "ups"),
        ("RR123456785DE", "shop.example", "dhl"),
        ("LX876543216NL", "shop.example", "postnl"),
        ("CX112233447CN", "shop.example", None),
        ("00340434161094042809", "fedex.com", "fedex"),
        ("00340434161094042809", "mail.post.at", "post_at"),
        ("00340434161094042809", "shop.example", None),
        # Das Format gewinnt vor der Domain.
        ("1Z0E12345678901235", "fedex.com", "ups"),
    ],
)
def test_carrier_ableitung(nummer: str, domain: str, erwartet: str | None):
    """Zusteller aus Nummernformat, sonst aus der Absender-Domain."""
    assert carrier_for(nummer, domain) == erwartet


def test_abgeleiteter_zusteller_bestimmt_die_id():
    """Eine UPS-Nummer aus einer Shop-Mail rastet auf die spaetere UPS-Mail ein."""
    shipments = PLUGIN.parse_all(_mail("Ihre Sendungsnummer: 1Z0E12345678901235"))

    assert [shipment.id for shipment in shipments] == ["ups_1z0e12345678901235"]
    assert shipments[0].carrier == "ups"
    assert shipments[0].sender == "Beispiel-Shop"


def test_zusteller_als_absender_ist_kein_haendler():
    """Schreibt FedEx selbst, ist FedEx nicht der Haendler."""
    mail = _mail("Tracking number: 00340434161094042809", from_address="noreply@fedex.com")

    shipment = PLUGIN.parse_all(mail)[0]

    assert shipment.carrier == "fedex"
    assert shipment.sender is None


def test_eindeutiger_status_wird_uebernommen():
    """Was sich erschliesst, wird gesetzt — sonst bleibt `unknown`."""
    shipment = PLUGIN.parse_all(_mail("Ihre Bestellung wurde versandt.\nLX876543216NL"))[0]

    assert shipment.status is ShipmentStatus.IN_TRANSIT


def test_bewertung_ist_immer_die_niedrigste():
    """Mit Nummer 1, ohne 0 — der Fallback ueberbietet nie ein anderes Plugin."""
    assert PLUGIN.matches(_mail("Trackingnummer LX876543216NL")) == 1
    assert PLUGIN.matches(_mail("Vielen Dank fuer Ihren Einkauf")) == 0
    assert PLUGIN.priority == -100
