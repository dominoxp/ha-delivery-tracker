"""Tests fuer das Carrier-Plugin Hermes.

`paketshop.eml` und `unterwegs_zum_paketshop.eml` sind aus echten Mails von
2026 abgeleitet, die uebrigen Mailtypen nachgebaut.
"""

import re
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from carrier_expectations import Loader, assert_fields, one_shipment
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.carriers.hermes import PLUGIN
from custom_components.delivery_tracker.mail import parse_mail
from custom_components.delivery_tracker.models import ShipmentStatus

URL = "https://www.myhermes.de/empfangen/sendungsverfolgung/sendungsinformation#"


@pytest.fixture(autouse=True)
def berlin_timezone() -> Generator[None]:
    """Zeitfenster sind lokal gemeint — hier durchgaengig Europe/Berlin."""
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(original)


ERWARTET: dict[str, dict[str, Any]] = {
    # Keine sichtbare Nummer — sie steht nur im Ziel des Verfolgen-Buttons.
    "sendungsankuendigung.eml": {
        "id": "hermes_h1001234567890123451",
        "tracking_number": "H1001234567890123451",
        "status": ShipmentStatus.LABEL_CREATED,
        "tracking_url": f"{URL}H1001234567890123451",
        "eta_start": datetime(2026, 7, 30, 22, 0, tzinfo=UTC),
        "eta_end": datetime(2026, 7, 31, 22, 0, tzinfo=UTC),
    },
    "zustellzeitfenster.eml": {
        "tracking_number": "01234567890123",
        "status": ShipmentStatus.OUT_FOR_DELIVERY,
        "sender": "Kaffeerösterei Nord",
        "eta_start": datetime(2026, 7, 31, 10, 0, tzinfo=UTC),
        "eta_end": datetime(2026, 7, 31, 14, 0, tzinfo=UTC),
    },
    "zustellung.eml": {
        "tracking_number": "0123456789012345",
        "status": ShipmentStatus.DELIVERED,
        "delivered_at": datetime(2026, 7, 31, 11, 17, tzinfo=UTC),
    },
    "zustellversuch.eml": {
        "tracking_number": "H1001234567890123468",
        "status": ShipmentStatus.DELIVERY_FAILED,
        "eta_start": datetime(2026, 8, 2, 22, 0, tzinfo=UTC),
        "eta_end": datetime(2026, 8, 3, 22, 0, tzinfo=UTC),
    },
    "nachbar.eml": {
        "tracking_number": "H1001234567890123475",
        "status": ShipmentStatus.DELIVERED,
        "pickup_location": "Familie Beispiel, Musterstraße 5, 12345 Musterstadt",
    },
    # Echtes Layout: Abholort als Block unter „PaketShop", Haendler im Satz.
    "paketshop.eml": {
        "tracking_number": "H1001234567890123482",
        "status": ShipmentStatus.READY_FOR_PICKUP,
        "sender": "Amazon",
        "pickup_location": "Schreibwaren Beispiel Bahnhofstr. 2 12345 Musterstadt",
    },
    # Echtes Layout: „liegt in Kuerze zur Abholung bereit" ist noch unterwegs.
    "unterwegs_zum_paketshop.eml": {
        "tracking_number": "H1001234567890123499",
        "status": ShipmentStatus.IN_TRANSIT,
        "sender": "Amazon",
        "pickup_location": "Schreibwaren Beispiel Bahnhofstr. 2 12345 Musterstadt",
        "eta_start": datetime(2026, 2, 18, 23, 0, tzinfo=UTC),
        "eta_end": datetime(2026, 2, 19, 23, 0, tzinfo=UTC),
    },
}


@pytest.mark.parametrize("datei", sorted(ERWARTET))
def test_mailtyp(email_fixture: Loader, datei: str):
    """Jeder Mailtyp liefert genau eine Sendung mit den erwarteten Feldern."""
    shipment = one_shipment(PLUGIN, email_fixture, f"hermes/{datei}")

    assert shipment.carrier == "hermes"
    assert_fields(shipment, ERWARTET[datei])


def test_nummer_kommt_nur_aus_dem_link(email_fixture: Loader):
    """Die Ankuendigung nennt die Nummer im Text nicht — der Link ist der Hauptpfad."""
    mail = parse_mail(email_fixture("hermes/sendungsankuendigung.eml"))
    sichtbar = re.sub(r"https?://\S+", "", mail.text)

    assert "H1001234567890123451" in mail.text
    assert "H1001234567890123451" not in sichtbar
    assert [shipment.tracking_number for shipment in PLUGIN.parse_all(mail)] == [
        "H1001234567890123451"
    ]
