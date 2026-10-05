"""Zuordnung echter Fixture-Mails zu den mitgelieferten Carrier-Plugins.

Die Plugin-Tests pruefen jeweils ihr eigenes Plugin. Hier steht die andere
Haelfte: dass sich die Plugins gegenseitig nicht die Mails wegnehmen, dass
Werbung von keinem angenommen wird und dass der generische Fallback genau
dann zum Zug kommt, wenn alle spezifischen Plugins leer bleiben.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from custom_components.delivery_tracker.carriers import CarrierRegistry, discover_plugins
from custom_components.delivery_tracker.mail import parse_mail

Loader = Callable[[str], bytes]

CARRIERS = Path(__file__).parent.parent / "custom_components" / "delivery_tracker" / "carriers"

# Die echte Registry, so wie HA sie laedt — inklusive `generic` und `example`.
REGISTRY = CarrierRegistry(
    discover_plugins(CARRIERS, package="custom_components.delivery_tracker.carriers")
)

FIXTURES = Path(__file__).parent / "fixtures" / "emails"


def _fixtures(carrier: str, *ausser: str) -> list[str]:
    """Alle Fixtures eines Zustellers ausser den genannten."""
    return sorted(
        f"{carrier}/{path.name}"
        for path in (FIXTURES / carrier).glob("*.eml")
        if path.name not in ausser
    )


def test_alle_zusteller_sind_geladen():
    """Ein Plugin mit kaputter `rules.yaml` wuerde hier still fehlen."""
    assert REGISTRY.carrier_ids == [
        "amazon",
        "dhl",
        "dpd",
        "example",
        "generic",
        "gls",
        "hermes",
        "ups",
    ]


@pytest.mark.parametrize(
    ("datei", "carrier"),
    [
        *[(datei, "dhl") for datei in _fixtures("dhl", "newsletter.eml")],
        *[(datei, "amazon") for datei in _fixtures("amazon", "werbung.eml")],
        *[(datei, "dpd") for datei in _fixtures("dpd")],
        *[(datei, "gls") for datei in _fixtures("gls")],
        *[(datei, "hermes") for datei in _fixtures("hermes")],
        *[(datei, "ups") for datei in _fixtures("ups")],
    ],
)
def test_mail_landet_beim_richtigen_zusteller(email_fixture: Loader, datei: str, carrier: str):
    """Shops nennen Zusteller, Zusteller nennen Pakete — trotzdem ist die Zuordnung eindeutig.

    Insbesondere wertet nie `generic` eine Mail aus, die ein spezifisches
    Plugin versteht.
    """
    resolution = REGISTRY.resolve(parse_mail(email_fixture(datei)))

    assert resolution.plugin is not None
    assert resolution.plugin.carrier_id == carrier
    assert resolution.shipments
    assert all(shipment.carrier == carrier for shipment in resolution.shipments)


@pytest.mark.parametrize("datei", ["dhl/newsletter.eml", "amazon/werbung.eml"])
def test_werbung_wird_von_keinem_plugin_angenommen(email_fixture: Loader, datei: str):
    """Ein Newsletter darf nicht einmal einen Kandidaten finden."""
    mail = parse_mail(email_fixture(datei))

    assert REGISTRY.match(mail) is None
    assert REGISTRY.parse_all(mail) == []


def test_echte_dpd_mail_geht_nicht_mehr_an_dhl(email_fixture: Loader):
    """Der Fall aus dem Nachtrag zu 1.3: „Ihr Paket" im Betreff brachte DHL 20 Punkte."""
    mail = parse_mail(email_fixture("dpd/unterwegs.eml"))

    matched = REGISTRY.match(mail)

    assert matched is not None
    assert matched[0].carrier_id == "dpd"


def test_fremd_beanspruchte_mail_landet_bei_generic(email_fixture: Loader):
    """DHL fuehlt sich wegen „Ihr Paket" zustaendig, findet aber nichts — dann `generic`."""
    mail = parse_mail(email_fixture("generic/fremd_beansprucht.eml"))

    best = REGISTRY.match(mail)
    resolution = REGISTRY.resolve(mail)

    assert best is not None
    assert best[0].carrier_id == "dhl"
    assert resolution.plugin is not None
    assert resolution.plugin.carrier_id == "generic"
    assert [shipment.id for shipment in resolution.shipments] == ["postnl_lx876543216nl"]


def test_unbekannter_zusteller_wird_von_generic_erfasst(email_fixture: Loader):
    """Eine Shop-Mail mit gueltiger Nummer eines Zustellers ohne Plugin."""
    resolution = REGISTRY.resolve(parse_mail(email_fixture("generic/unbekannter_zusteller.eml")))

    assert resolution.plugin is not None
    assert resolution.plugin.carrier_id == "generic"
    assert [shipment.tracking_number for shipment in resolution.shipments] == ["CX112233447CN"]


@pytest.mark.parametrize("datei", ["generic/rechnung.eml", "generic/shop_ohne_nummer.eml"])
def test_shop_mails_ohne_trackingnummer_ergeben_keine_sendung(email_fixture: Loader, datei: str):
    """Rechnungs-, Kunden- und Bestellnummern werden von keinem Plugin zur Sendung.

    `shop_ohne_nummer.eml` ist die echte Shop-Mail aus dem Nachtrag zu 1.3:
    Amazon fuehlt sich wegen „Ihre Bestellung" zustaendig, findet aber keine
    Amazon-Bestellnummer — und `generic` keine Trackingnummer.
    """
    assert REGISTRY.parse_all(parse_mail(email_fixture(datei))) == []
