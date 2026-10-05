"""Gemeinsamer Pruefablauf fuer die rein regelbasierten Carrier-Plugins aus Step 1.4.

DPD, GLS, Hermes und UPS sind gleich gebaut; ihre Tests unterscheiden sich nur
in den erwarteten Werten. Die stehen in den Testdateien als Tabelle, die
Pruefung selbst einmal hier.
"""

from collections.abc import Callable
from typing import Any

from custom_components.delivery_tracker.carriers import CarrierPlugin
from custom_components.delivery_tracker.mail import parse_mail
from custom_components.delivery_tracker.models import Shipment

Loader = Callable[[str], bytes]


def one_shipment(plugin: CarrierPlugin, email_fixture: Loader, path: str) -> Shipment:
    """Die eine Sendung einer Fixture-Mail."""
    shipments = plugin.parse_all(parse_mail(email_fixture(path)))
    assert len(shipments) == 1, [shipment.id for shipment in shipments]
    return shipments[0]


def assert_fields(shipment: Shipment, expected: dict[str, Any]) -> None:
    """Alle erwarteten Felder vergleichen; nicht genannte Felder bleiben ungeprueft.

    Ein ausdrueckliches `None` in der Erwartung prueft, dass das Feld leer
    bleibt — lieber ein fehlendes Feld als ein falsches.
    """
    actual = {name: getattr(shipment, name) for name in expected}
    assert actual == expected
