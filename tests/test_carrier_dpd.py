"""Tests fuer das Carrier-Plugin DPD.

`unterwegs.eml` ist aus einer echten myDPD-Mail abgeleitet, die uebrigen
Mailtypen sind nachgebaut.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from carrier_expectations import Loader, assert_fields, one_shipment
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.carriers.dpd import PLUGIN
from custom_components.delivery_tracker.mail import parse_mail
from custom_components.delivery_tracker.models import ShipmentStatus

URL = "https://tracking.dpd.de/status/de_DE/parcel/"


@pytest.fixture(autouse=True)
def berlin_timezone() -> Generator[None]:
    """Zeitfenster sind lokal gemeint — hier durchgaengig Europe/Berlin."""
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


ERWARTET: dict[str, dict[str, Any]] = {
    # Echte Mail: Die Nummer steht im Betreff ohne Ankuendigung und im Link.
    "unterwegs.eml": {
        "id": "dpd_09981122334455",
        "tracking_number": "09981122334455",
        "status": ShipmentStatus.IN_TRANSIT,
        "tracking_url": f"{URL}09981122334455",
        "eta_start": None,
    },
    # In Vierergruppen gesetzte Nummer; das Fenster gilt fuer heute.
    "zustellzeitfenster.eml": {
        "tracking_number": "09981122334456",
        "status": ShipmentStatus.OUT_FOR_DELIVERY,
        "sender": "Kaffeerösterei Nord",
        "eta_start": datetime(2026, 7, 29, 8, 30, tzinfo=UTC),
        "eta_end": datetime(2026, 7, 29, 9, 30, tzinfo=UTC),
    },
    "zustellung.eml": {
        "tracking_number": "09981122334457",
        "status": ShipmentStatus.DELIVERED,
        "delivered_at": datetime(2026, 7, 29, 12, 12, tzinfo=UTC),
        "destination_raw": "Musterstraße 1, 12345 Musterstadt",
    },
    "zustellversuch.eml": {
        "tracking_number": "09981122334458",
        "status": ShipmentStatus.DELIVERY_FAILED,
        "eta_start": datetime(2026, 7, 29, 22, 0, tzinfo=UTC),
        "eta_end": datetime(2026, 7, 30, 22, 0, tzinfo=UTC),
        "delivered_at": None,
    },
    # Ablage beim Nachbarn ist zugestellt; der Nachbar ist der Abholort.
    "nachbar.eml": {
        "tracking_number": "09981122334459",
        "status": ShipmentStatus.DELIVERED,
        "pickup_location": "Familie Beispiel, Musterstraße 3, 12345 Musterstadt",
    },
    "paketshop.eml": {
        "tracking_number": "09981122334460",
        "status": ShipmentStatus.READY_FOR_PICKUP,
        "pickup_location": "DPD Pickup Paketshop Kiosk am Markt, Marktplatz 3, 12345 Musterstadt",
        "pickup_code": "4711AB",
    },
    # Der Shop schreibt selbst; DPD steckt nur in Link und Text.
    "im_auftrag_des_shops.eml": {
        "tracking_number": "09981122334461",
        "status": ShipmentStatus.IN_TRANSIT,
        "order_number": "KN-2026-4711",
    },
}


@pytest.mark.parametrize("datei", sorted(ERWARTET))
def test_mailtyp(email_fixture: Loader, datei: str):
    """Jeder Mailtyp liefert genau eine Sendung mit den erwarteten Feldern."""
    shipment = one_shipment(PLUGIN, email_fixture, f"dpd/{datei}")

    assert shipment.carrier == "dpd"
    assert_fields(shipment, ERWARTET[datei])


def test_postleitzahl_im_link_ist_keine_sendung(email_fixture: Loader):
    """Der echte Verfolgen-Link traegt `zip=` — das darf keine zweite Sendung werden."""
    mail = parse_mail(email_fixture("dpd/unterwegs.eml"))

    assert [shipment.tracking_number for shipment in PLUGIN.parse_all(mail)] == ["09981122334455"]


def test_echte_mail_wird_hoch_bewertet(email_fixture: Loader):
    """Domain, Betreff, Nummer und Firmenzeile kommen zusammen."""
    assert PLUGIN.matches(parse_mail(email_fixture("dpd/unterwegs.eml"))) >= 80
