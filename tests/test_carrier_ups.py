"""Tests fuer das Carrier-Plugin UPS (nachgebaute Mails, deutsch und englisch)."""

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from carrier_expectations import Loader, assert_fields, one_shipment
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.carriers.ups import PLUGIN
from custom_components.delivery_tracker.mail import MailMessage
from custom_components.delivery_tracker.models import ShipmentStatus

URL = "https://www.ups.com/track?loc=de_DE&tracknum="


@pytest.fixture(autouse=True)
def berlin_timezone() -> Generator[None]:
    """Zeitfenster sind lokal gemeint — hier durchgaengig Europe/Berlin."""
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


ERWARTET: dict[str, dict[str, Any]] = {
    "versandetikett.eml": {
        "id": "ups_1z0e12345678901235",
        "tracking_number": "1Z0E12345678901235",
        "status": ShipmentStatus.LABEL_CREATED,
        "sender": "Kaffeerösterei Nord",
        "destination_raw": "Musterstraße 1, 12345 Musterstadt",
        "tracking_url": f"{URL}1Z0E12345678901235",
        "eta_start": datetime(2026, 7, 29, 22, 0, tzinfo=UTC),
        "eta_end": datetime(2026, 7, 30, 22, 0, tzinfo=UTC),
    },
    # Nummer in Gruppen gesetzt, wie UPS sie im Text darstellt.
    "zustellzeitfenster.eml": {
        "tracking_number": "1Z0E12345678901244",
        "status": ShipmentStatus.OUT_FOR_DELIVERY,
        "eta_start": datetime(2026, 7, 30, 8, 30, tzinfo=UTC),
        "eta_end": datetime(2026, 7, 30, 12, 30, tzinfo=UTC),
    },
    # Englisch; das US-Datum wird nicht geraten, es gilt der Zeitpunkt der Mail.
    "zugestellt_englisch.eml": {
        "tracking_number": "1Z0E12345678901253",
        "status": ShipmentStatus.DELIVERED,
        "sender": "Kaffeerösterei Nord",
        "destination_raw": "Musterstraße 1, 12345 Musterstadt, DE",
        "delivered_at": datetime(2026, 7, 30, 12, 31, tzinfo=UTC),
    },
    "unterwegs_englisch.eml": {
        "tracking_number": "1Z0E12345678901262",
        "status": ShipmentStatus.IN_TRANSIT,
        "eta_start": None,
        "eta_end": None,
    },
    "zustellversuch.eml": {
        "tracking_number": "1Z0E12345678901271",
        "status": ShipmentStatus.DELIVERY_FAILED,
        "eta_start": datetime(2026, 7, 30, 22, 0, tzinfo=UTC),
    },
    "nachbar.eml": {
        "tracking_number": "1Z0E12345678901280",
        "status": ShipmentStatus.DELIVERED,
        "pickup_location": "Familie Beispiel, Musterstraße 5, 12345 Musterstadt",
    },
    # „An den Access Point zugestellt" heisst fuer den Empfaenger: abholen.
    "access_point.eml": {
        "tracking_number": "1ZA6B7C8D9E0F12349",
        "status": ShipmentStatus.READY_FOR_PICKUP,
        "pickup_location": "UPS Access Point Kiosk am Markt, Marktplatz 3, 12345 Musterstadt",
        "delivered_at": None,
    },
}


@pytest.mark.parametrize("datei", sorted(ERWARTET))
def test_mailtyp(email_fixture: Loader, datei: str):
    """Jeder Mailtyp liefert genau eine Sendung mit den erwarteten Feldern."""
    shipment = one_shipment(PLUGIN, email_fixture, f"ups/{datei}")

    assert shipment.carrier == "ups"
    assert_fields(shipment, ERWARTET[datei])


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Your package is out for delivery.", ShipmentStatus.OUT_FOR_DELIVERY),
        ("We missed you. We'll try again tomorrow.", ShipmentStatus.DELIVERY_FAILED),
        ("Your package was delivered to a UPS Access Point.", ShipmentStatus.READY_FOR_PICKUP),
        ("Your delivery has been delayed.", ShipmentStatus.DELAYED),
        ("Shipping label created.", ShipmentStatus.LABEL_CREATED),
    ],
)
def test_englische_status(text: str, status: ShipmentStatus):
    """UPS schreibt deutschen Empfaengern auch englisch."""
    mail = MailMessage(
        message_id="<ups-en@ups.com>",
        subject="UPS Update",
        text=f"{text}\nTracking Number: 1Z0E12345678901235",
        from_address="pkginfo@ups.com",
    )

    assert PLUGIN.parse_all(mail)[0].status is status


def test_falsche_pruefziffer_ergibt_keine_sendung():
    """Bei UPS entscheidet die Pruefziffer, nicht das Umfeld."""
    mail = MailMessage(
        message_id="<ups-kaputt@ups.com>",
        subject="UPS Update",
        text="Sendungsnummer: 1Z0E12345678901236",
        from_address="mcinfo@ups.com",
    )

    assert PLUGIN.parse_all(mail) == []
