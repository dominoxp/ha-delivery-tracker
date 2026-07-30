"""Tests fuer Datenmodell und Serialisierung."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.delivery_tracker.models import (
    MODEL_VERSION,
    Destination,
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
    SourceRecord,
)

BERLIN = ZoneInfo("Europe/Berlin")


def _full_shipment() -> Shipment:
    """Eine Sendung mit allen Feldern gefuellt."""
    return Shipment(
        id="dhl_00340434161094042557",
        carrier="dhl",
        tracking_number="00340434161094042557",
        status=ShipmentStatus.OUT_FOR_DELIVERY,
        status_text="Die Sendung wurde in das Zustellfahrzeug geladen",
        eta_start=datetime(2026, 7, 28, 10, 0, tzinfo=BERLIN),
        eta_end=datetime(2026, 7, 28, 14, 0, tzinfo=BERLIN),
        delivered_at=None,
        destination_id="zuhause",
        destination_raw="Musterstrasse 1, 12345 Musterstadt",
        sender="Amazon",
        description="Kaffeebohnen 1kg (+1 weiterer Artikel)",
        items=["Kaffeebohnen 1kg", "Filtertüten Größe 4"],
        order_number="302-1234567-1234567",
        pickup_location="Packstation 123",
        pickup_code="9876",
        tracking_url="https://example.invalid/track/00340434161094042557",
        events=[
            ShipmentEvent(
                timestamp=datetime(2026, 7, 27, 18, 30, tzinfo=BERLIN),
                status=ShipmentStatus.IN_TRANSIT,
                text="Im Paketzentrum bearbeitet",
                location="Hamburg",
            ),
            ShipmentEvent(
                timestamp=datetime(2026, 7, 28, 7, 5, tzinfo=UTC),
                status=ShipmentStatus.OUT_FOR_DELIVERY,
            ),
        ],
        sources=[
            SourceRecord(
                source_id="imap",
                fetched_at=datetime(2026, 7, 28, 7, 6, tzinfo=UTC),
                reference="<abc@mail.example>",
            )
        ],
        first_seen=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        last_updated=datetime(2026, 7, 28, 7, 6, tzinfo=UTC),
    )


def test_status_rangfolge_und_abschluss():
    """Die Deklarationsreihenfolge ist die Fortschritts-Rangfolge."""
    assert ShipmentStatus.UNKNOWN.rank < ShipmentStatus.ORDERED.rank
    assert ShipmentStatus.IN_TRANSIT.rank < ShipmentStatus.OUT_FOR_DELIVERY.rank
    assert ShipmentStatus.OUT_FOR_DELIVERY.rank < ShipmentStatus.DELIVERED.rank

    assert ShipmentStatus.DELIVERED.is_closed
    assert ShipmentStatus.RETURNED.is_closed
    assert ShipmentStatus.DELIVERY_FAILED.is_closed
    assert not ShipmentStatus.IN_TRANSIT.is_closed


def test_shipment_roundtrip():
    """to_dict -> from_dict ergibt ein gleichwertiges Objekt."""
    shipment = _full_shipment()

    restored = Shipment.from_dict(shipment.to_dict())

    assert restored == shipment
    assert restored.eta_start == datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    assert restored.eta_start.utcoffset().total_seconds() == 0
    assert restored.events[0].location == "Hamburg"
    assert restored.sources[0].reference == "<abc@mail.example>"
    # Die Artikelliste ist das Bindeglied fuer Step 3.2 und darf nicht
    # unterwegs verloren gehen.
    assert restored.items == ["Kaffeebohnen 1kg", "Filtertüten Größe 4"]


def test_roundtrip_mit_leeren_listen_und_none():
    """Minimal befuellte Sendungen ueberstehen den Roundtrip ebenfalls."""
    shipment = Shipment(
        id="generic_order_shop_4711",
        carrier="generic",
        first_seen=datetime(2026, 7, 1, tzinfo=UTC),
        last_updated=datetime(2026, 7, 1, tzinfo=UTC),
    )

    restored = Shipment.from_dict(shipment.to_dict())

    assert restored == shipment
    assert restored.events == []
    assert restored.sources == []
    assert restored.items == []
    assert restored.tracking_number is None
    assert restored.status is ShipmentStatus.UNKNOWN


def test_serialisierung_traegt_schema_version():
    """Jede Serialisierung enthaelt die Schema-Version."""
    assert _full_shipment().to_dict()["version"] == MODEL_VERSION
    assert Destination(id="x", name="X").to_dict()["version"] == MODEL_VERSION


def test_neuere_schema_version_wird_abgelehnt():
    """Daten aus einer neueren Version werden nicht blind gelesen."""
    data = _full_shipment().to_dict()
    data["version"] = MODEL_VERSION + 1

    with pytest.raises(ValueError, match="Schema-Version"):
        Shipment.from_dict(data)


def test_naive_zeitstempel_sind_ein_fehler():
    """Naive Zeitstempel werden nicht stillschweigend interpretiert."""
    with pytest.raises(ValueError, match="timezone-aware"):
        Shipment(
            id="dhl_1",
            carrier="dhl",
            first_seen=datetime(2026, 7, 1),
            last_updated=datetime(2026, 7, 1, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        ShipmentEvent(timestamp=datetime(2026, 7, 1))


def test_as_summary_ist_kompakt():
    """Die Attributliste traegt keine Event-Historie."""
    summary = _full_shipment().as_summary()

    assert "events" not in summary
    assert "sources" not in summary
    assert summary["id"] == "dhl_00340434161094042557"
    assert summary["status"] == "out_for_delivery"
    assert summary["eta_start"] == "2026-07-28T08:00:00+00:00"
    assert summary["destination_id"] == "zuhause"


def test_shipment_aktiv_und_abgeschlossen():
    """Der Abschluss-Zeitpunkt haengt am Zustellzeitpunkt, sonst an last_updated."""
    shipment = _full_shipment()
    assert shipment.is_active

    shipment.status = ShipmentStatus.DELIVERED
    shipment.delivered_at = datetime(2026, 7, 28, 11, 0, tzinfo=UTC)
    assert shipment.is_closed
    assert shipment.closed_at == datetime(2026, 7, 28, 11, 0, tzinfo=UTC)

    shipment.delivered_at = None
    assert shipment.closed_at == shipment.last_updated


def test_destination_roundtrip():
    """Zielorte inklusive Match-Regeln ueberstehen den Roundtrip."""
    destination = Destination(
        id="buero",
        name="Buero",
        recipient_names=["Erika Mustermann"],
        streets=["Firmenweg 7"],
        postal_codes=["12345"],
        packstations=["123"],
        emails=["erika@example.invalid"],
    )

    restored = Destination.from_dict(destination.to_dict())

    assert restored == destination
