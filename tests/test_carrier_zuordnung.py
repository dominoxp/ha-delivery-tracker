"""Zuordnung echter Fixture-Mails zu den mitgelieferten Carrier-Plugins.

Die Plugin-Tests pruefen jeweils ihr eigenes Plugin. Hier steht die andere
Haelfte: dass sich die Plugins gegenseitig nicht die Mails wegnehmen und dass
Werbung von keinem angenommen wird.
"""

from collections.abc import Callable

import pytest

from custom_components.delivery_tracker.carriers import CarrierRegistry
from custom_components.delivery_tracker.carriers.amazon import PLUGIN as AMAZON
from custom_components.delivery_tracker.carriers.dhl import PLUGIN as DHL
from custom_components.delivery_tracker.carriers.example import PLUGIN as EXAMPLE
from custom_components.delivery_tracker.mail import parse_mail

Loader = Callable[[str], bytes]

REGISTRY = CarrierRegistry([AMAZON, DHL, EXAMPLE])


@pytest.mark.parametrize(
    ("datei", "carrier"),
    [
        ("dhl/sendungsankuendigung.eml", "dhl"),
        ("dhl/zustellzeitfenster.eml", "dhl"),
        ("dhl/zustellung.eml", "dhl"),
        ("dhl/nachbar.eml", "dhl"),
        ("dhl/packstation.eml", "dhl"),
        ("dhl/filiale.eml", "dhl"),
        ("dhl/zustellversuch.eml", "dhl"),
        ("dhl/wunschtag.eml", "dhl"),
        ("dhl/retoure.eml", "dhl"),
        ("dhl/einschreiben.eml", "dhl"),
        ("dhl/express_kommt_heute.eml", "dhl"),
        ("dhl/express_verspaetet.eml", "dhl"),
        ("dhl/express_filiale.eml", "dhl"),
        ("amazon/bestellbestaetigung.eml", "amazon"),
        ("amazon/versandbestaetigung.eml", "amazon"),
        ("amazon/versand_sammelpaket.eml", "amazon"),
        ("amazon/locker_bereit.eml", "amazon"),
        ("amazon/locker_sammelpaket.eml", "amazon"),
        ("amazon/locker_abgeholt.eml", "amazon"),
        ("amazon/logistics_zeitfenster.eml", "amazon"),
        ("amazon/verspaetet.eml", "amazon"),
        ("amazon/zugestellt.eml", "amazon"),
    ],
)
def test_mail_landet_beim_richtigen_zusteller(email_fixture: Loader, datei: str, carrier: str):
    """Amazon nennt DHL und DHL nennt Pakete — trotzdem ist die Zuordnung eindeutig."""
    matched = REGISTRY.match(parse_mail(email_fixture(datei)))

    assert matched is not None
    assert matched[0].carrier_id == carrier

    shipments = REGISTRY.parse_all(parse_mail(email_fixture(datei)))
    assert shipments
    assert all(shipment.carrier == carrier for shipment in shipments)


@pytest.mark.parametrize("datei", ["dhl/newsletter.eml", "amazon/werbung.eml"])
def test_werbung_wird_von_keinem_plugin_angenommen(email_fixture: Loader, datei: str):
    """Ein Newsletter darf nicht einmal einen Kandidaten finden."""
    mail = parse_mail(email_fixture(datei))

    assert REGISTRY.match(mail) is None
    assert REGISTRY.parse_all(mail) == []
