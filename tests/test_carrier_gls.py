"""Tests fuer das Carrier-Plugin GLS (nachgebaute Mails, keine echte lag vor)."""

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from carrier_expectations import Loader, assert_fields, one_shipment
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.carriers.gls import PLUGIN
from custom_components.delivery_tracker.mail import MailMessage
from custom_components.delivery_tracker.models import ShipmentStatus

URL = "https://gls-group.com/DE/de/paketverfolgung?match="


@pytest.fixture(autouse=True)
def berlin_timezone() -> Generator[None]:
    """Zeitfenster sind lokal gemeint — hier durchgaengig Europe/Berlin."""
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


ERWARTET: dict[str, dict[str, Any]] = {
    # Paketnummer und Track ID in derselben Mail — eine Sendung, die Paketnummer gewinnt.
    "sendungsankuendigung.eml": {
        "id": "gls_50123456789",
        "tracking_number": "50123456789",
        "status": ShipmentStatus.LABEL_CREATED,
        "sender": "Kaffeerösterei Nord",
        "tracking_url": f"{URL}50123456789",
        "eta_start": datetime(2026, 7, 29, 22, 0, tzinfo=UTC),
        "eta_end": datetime(2026, 7, 30, 22, 0, tzinfo=UTC),
    },
    # HTML; die Nummer steht nur im Verfolgen-Link.
    "zustellzeitfenster.eml": {
        "tracking_number": "50123456790",
        "status": ShipmentStatus.OUT_FOR_DELIVERY,
        "sender": "Kaffeerösterei Nord",
        "eta_start": datetime(2026, 7, 30, 11, 0, tzinfo=UTC),
        "eta_end": datetime(2026, 7, 30, 13, 0, tzinfo=UTC),
    },
    "zustellung.eml": {
        "tracking_number": "50123456791",
        "status": ShipmentStatus.DELIVERED,
        "delivered_at": datetime(2026, 7, 30, 11, 41, tzinfo=UTC),
    },
    # Nur die Track ID — dann ist sie die Nummer.
    "zustellversuch.eml": {
        "tracking_number": "ZC9M3T5R",
        "status": ShipmentStatus.DELIVERY_FAILED,
        "eta_start": datetime(2026, 7, 30, 22, 0, tzinfo=UTC),
    },
    "nachbar.eml": {
        "tracking_number": "50123456792",
        "status": ShipmentStatus.DELIVERED,
        "pickup_location": "Familie Beispiel, Musterstraße 3, 12345 Musterstadt",
    },
    "paketshop.eml": {
        "tracking_number": "50123456793",
        "status": ShipmentStatus.READY_FOR_PICKUP,
        "pickup_location": (
            "GLS PaketShop Schreibwaren Beispiel, Bahnhofstraße 2, 12345 Musterstadt"
        ),
    },
}


@pytest.mark.parametrize("datei", sorted(ERWARTET))
def test_mailtyp(email_fixture: Loader, datei: str):
    """Jeder Mailtyp liefert genau eine Sendung mit den erwarteten Feldern."""
    shipment = one_shipment(PLUGIN, email_fixture, f"gls/{datei}")

    assert shipment.carrier == "gls"
    assert_fields(shipment, ERWARTET[datei])


def test_track_id_braucht_ihre_beschriftung():
    """Acht Zeichen mit Ziffer gibt es in jeder Mail — ohne „Track ID" zaehlen sie nicht."""
    mail = MailMessage(
        message_id="<gls-gutschein@gls-group.eu>",
        subject="Ihr GLS Paket",
        text="Ihr Gutschein: AB12CD34\nGutschein einloesen: https://gls-pakete.de/aktion/AB12CD34",
        from_address="noreply@gls-group.eu",
    )

    assert PLUGIN.tracking_numbers(mail) == []
    assert PLUGIN.parse_all(mail) == []
