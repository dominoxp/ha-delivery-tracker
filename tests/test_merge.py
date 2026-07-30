"""Tests fuer ID-Bildung und Merge-Regeln."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.delivery_tracker.const import (
    MAX_EVENTS_PER_SHIPMENT,
    MAX_ITEMS_PER_SHIPMENT,
    SOURCE_IMAP,
    SOURCE_SEVENTEENTRACK,
)
from custom_components.delivery_tracker.merge import (
    build_shipment_id,
    has_status_changed,
    merge_events,
    merge_items,
    merge_shipments,
    tracking_id_token,
)
from custom_components.delivery_tracker.models import (
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
    SourceRecord,
)

T0 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)


def _mail_shipment(**overrides: object) -> Shipment:
    """Sicht aus einer Mail: viel Kontext, Status aus dem Mailtext."""
    values: dict[str, object] = {
        "id": "dhl_jjd00998899",
        "carrier": "dhl",
        "tracking_number": "JJD 0099 8899",
        "status": ShipmentStatus.IN_TRANSIT,
        "status_text": "Die Sendung ist auf dem Weg",
        "sender": "Amazon",
        "description": "Kaffeebohnen 1kg",
        "destination_raw": "Musterstrasse 1, 12345 Musterstadt",
        "sources": [SourceRecord(source_id=SOURCE_IMAP, fetched_at=T1)],
        "first_seen": T0,
        "last_updated": T1,
    }
    values.update(overrides)
    return Shipment(**values)  # type: ignore[arg-type]


def _api_shipment(**overrides: object) -> Shipment:
    """Sicht aus der API: autoritativer Status, kaum Kontext."""
    values: dict[str, object] = {
        "id": "dhl_jjd00998899",
        "carrier": "dhl",
        "tracking_number": "JJD00998899",
        "status": ShipmentStatus.DELIVERED,
        "status_text": "Delivered",
        "delivered_at": T2,
        "sources": [SourceRecord(source_id=SOURCE_SEVENTEENTRACK, fetched_at=T2)],
        "first_seen": T2,
        "last_updated": T2,
    }
    values.update(overrides)
    return Shipment(**values)  # type: ignore[arg-type]


def test_merge_items_vereinigt_statt_zu_ersetzen():
    """Keine Quelle nennt bewusst weniger Artikel — eine kuerzere Liste fehlt nur.

    Amazon zaehlt dieselben Artikel in Versand- und Abholmail in verschiedener
    Reihenfolge auf; die Vereinigung macht beide Sichten vergleichbar.
    """
    versand = ["USB-C Kabel 2 m", "Schutzhülle transparent"]
    abholung = ["Schutzhülle transparent", "USB-C Kabel 2 m"]

    merged = merge_items(versand, abholung)

    assert merged == versand
    assert merge_items(versand, []) == versand
    assert merge_items([], abholung) == abholung


def test_merge_items_ergaenzt_fehlende_artikel():
    """Kennt eine Sicht einen Artikel mehr, bleibt er erhalten."""
    merged = merge_items(["Kabel"], ["Hülle", "Kabel", "Adapter"])

    assert merged == ["Kabel", "Hülle", "Adapter"]


def test_merge_items_ignoriert_schreibweise_und_leerraum():
    """Dieselbe Bezeichnung darf nicht doppelt in der Liste landen."""
    assert merge_items(["USB-C  Kabel"], ["usb-c kabel"]) == ["USB-C Kabel"]


def test_merge_items_wird_gedeckelt():
    """Eine sehr grosse Bestellung darf den Store nicht aufblaehen."""
    viele = [f"Artikel {index}" for index in range(MAX_ITEMS_PER_SHIPMENT + 10)]

    assert len(merge_items(viele, [])) == MAX_ITEMS_PER_SHIPMENT


def test_merge_shipments_fuehrt_artikellisten_zusammen():
    """Beim Zusammenfuehren zweier Sichten bleiben alle Artikel erhalten."""
    merged = merge_shipments(
        _mail_shipment(items=["Kaffeebohnen 1kg"]),
        _api_shipment(items=["Filtertüten", "Kaffeebohnen 1kg"]),
    )

    assert merged.items == ["Kaffeebohnen 1kg", "Filtertüten"]


def test_normalisierte_trackingnummer_ergibt_stabile_id():
    """Schreibweise und Trennzeichen duerfen die ID nicht veraendern."""
    assert tracking_id_token("JJD 0099-8899") == "jjd00998899"
    assert build_shipment_id("DHL", "JJD 0099 8899") == "dhl_jjd00998899"
    assert build_shipment_id("dhl", "jjd00998899") == "dhl_jjd00998899"


def test_id_ohne_trackingnummer_aus_bestellung():
    """Bestellbestaetigungen bekommen eine ID aus Haendler und Bestellnummer."""
    shipment_id = build_shipment_id(
        "amazon", sender="Amazon.de", order_number="302-1234567-1234567"
    )

    assert shipment_id == "amazon_order_amazon_de_302_1234567_1234567"


def test_id_ohne_jede_kennung_ist_ein_fehler():
    """Ohne Tracking- und Bestellnummer gibt es keine stabile ID."""
    with pytest.raises(ValueError, match="Trackingnummer"):
        build_shipment_id("dhl")


def test_api_ueberschreibt_mail_status():
    """Der weiter fortgeschrittene API-Status setzt sich durch."""
    merged = merge_shipments(_mail_shipment(), _api_shipment())

    assert merged.status is ShipmentStatus.DELIVERED
    assert merged.status_text == "Delivered"
    assert merged.delivered_at == T2
    assert merged.last_updated == T2
    assert merged.first_seen == T0


def test_mail_kontextfelder_ueberleben_die_api():
    """Felder, die nur die Mail kennt, werden nie mit Leere ueberschrieben."""
    merged = merge_shipments(_mail_shipment(), _api_shipment())

    assert merged.sender == "Amazon"
    assert merged.description == "Kaffeebohnen 1kg"
    assert merged.destination_raw == "Musterstrasse 1, 12345 Musterstadt"
    assert merged.source_ids == [SOURCE_IMAP, SOURCE_SEVENTEENTRACK]


def test_status_rueckschritt_wird_verworfen():
    """Eine nachzuegelnde Mail darf einen fortgeschrittenen Status nicht zuruecksetzen."""
    current = _api_shipment()
    late_mail = _mail_shipment(status=ShipmentStatus.IN_TRANSIT, last_updated=T1)

    merged = merge_shipments(current, late_mail)

    assert merged.status is ShipmentStatus.DELIVERED
    assert merged.status_text == "Delivered"
    assert merged.delivered_at == T2
    # Der Kontext der Mail kommt trotzdem an.
    assert merged.description == "Kaffeebohnen 1kg"


def test_returned_gewinnt_auch_gegen_delivered():
    """Fachlich echte Rueckschritte setzen sich immer durch."""
    current = _api_shipment()
    retoure = _mail_shipment(
        status=ShipmentStatus.RETURNED,
        status_text="Sendung geht zurueck an den Absender",
        last_updated=T1,
    )

    merged = merge_shipments(current, retoure)

    assert merged.status is ShipmentStatus.RETURNED
    assert merged.status_text == "Sendung geht zurueck an den Absender"
    assert merged.delivered_at is None


def test_hoehere_quelle_gewinnt_bei_gleichem_status():
    """Bei gleichem Rang entscheidet die Wertigkeit der Quelle."""
    mail = _mail_shipment(
        status=ShipmentStatus.DELIVERED,
        status_text="Zugestellt an Nachbarn",
        last_updated=T2,
    )
    api = _api_shipment(last_updated=T2)

    merged = merge_shipments(mail, api)

    assert merged.status_text == "Delivered"


def test_events_werden_dedupliziert_und_sortiert():
    """Gleiche Events aus zwei Quellen ergeben einen Eintrag — den ausfuehrlicheren."""
    aus_mail = [
        ShipmentEvent(timestamp=T1, status=ShipmentStatus.IN_TRANSIT, text="Im Paketzentrum"),
        ShipmentEvent(timestamp=T2, status=ShipmentStatus.DELIVERED, text="Zugestellt"),
    ]
    aus_api = [
        ShipmentEvent(timestamp=T1, status=ShipmentStatus.IN_TRANSIT),
        ShipmentEvent(timestamp=T0, status=ShipmentStatus.LABEL_CREATED, text="Label erstellt"),
    ]

    merged = merge_events(aus_mail, aus_api)

    assert [event.timestamp for event in merged] == [T0, T1, T2]
    assert merged[1].text == "Im Paketzentrum"


def test_event_historie_wird_gedeckelt():
    """Die Event-Liste laeuft dem Store nicht davon."""
    viele = [
        ShipmentEvent(timestamp=T0 + timedelta(minutes=index), status=ShipmentStatus.IN_TRANSIT)
        for index in range(MAX_EVENTS_PER_SHIPMENT + 20)
    ]

    merged = merge_events([], viele)

    assert len(merged) == MAX_EVENTS_PER_SHIPMENT
    assert merged[-1].timestamp == viele[-1].timestamp


def test_merge_laesst_die_eingaben_unveraendert():
    """Der Merge arbeitet ohne Seiteneffekte."""
    mail = _mail_shipment()
    api = _api_shipment()

    merge_shipments(mail, api)

    assert mail.status is ShipmentStatus.IN_TRANSIT
    assert mail.events == []
    assert api.sender is None


def test_merge_verlangt_gleiche_id():
    """Zwei verschiedene Sendungen werden nie zusammengefuehrt."""
    with pytest.raises(ValueError, match="verschiedener ID"):
        merge_shipments(_mail_shipment(), _api_shipment(id="dhl_andere"))


def test_statuswechsel_erkennung():
    """Grundlage der spaeteren HA-Events."""
    mail = _mail_shipment()
    api = _api_shipment()

    assert has_status_changed(None, mail) is True
    assert has_status_changed(mail, merge_shipments(mail, api)) is True
    assert has_status_changed(mail, merge_shipments(mail, _mail_shipment())) is False
    assert has_status_changed(None, Shipment(id="x", carrier="dhl")) is False
