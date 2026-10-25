"""Getrennt gefuehrte Amazon-Sendungen derselben Bestellung zusammenfuehren (Step 3.2).

Der Fall stammt aus einem echten Postfach: Zur Infrarotheizung kamen
Bestellbestaetigung, Versandmail (mit `shipmentId`), „In Zustellung" und
„Geliefert" (ohne `shipmentId`). Daraus wurden drei Sendungen; die sichtbare
blieb auf „Unterwegs", die zugestellte lief unbemerkt nebenher.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.delivery_tracker.carriers.amazon import PLUGIN
from custom_components.delivery_tracker.const import EVENT_SHIPMENT_DELIVERED, SOURCE_IMAP
from custom_components.delivery_tracker.mail import parse_mail
from custom_components.delivery_tracker.merge import merge_target, package_anchor
from custom_components.delivery_tracker.models import (
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
    SourceRecord,
)
from custom_components.delivery_tracker.store import ShipmentStore
from tests.test_manage import setup_with

ORDER = "304-1496979-1480346"
HEIZUNG = "Infrarotheizung Wandmontage 350W"
T0 = datetime(2026, 10, 18, 20, 39, tzinfo=dt_util.UTC)


def _amazon(
    suffix: str,
    status: ShipmentStatus,
    *,
    days: float,
    items: tuple[str, ...] = (HEIZUNG,),
    order: str = ORDER,
) -> Shipment:
    when = T0 + timedelta(days=days)
    order_slug = order.replace("-", "_")
    return Shipment(
        id=f"amazon_order_{order_slug}{'_' + suffix if suffix else ''}",
        carrier="amazon",
        status=status,
        order_number=order,
        items=list(items),
        description=items[0] if items else None,
        delivered_at=when if status is ShipmentStatus.DELIVERED else None,
        events=[ShipmentEvent(timestamp=when, status=status)],
        sources=[SourceRecord(source_id=SOURCE_IMAP, fetched_at=when, reference=f"<{suffix}>")],
        first_seen=when,
        last_updated=when,
    )


@pytest.fixture(autouse=True)
def uhr(freezer: FrozenDateTimeFactory) -> None:
    """Zwei Tage nach der Zustellung — weit vor jeder Retention."""
    freezer.move_to(T0 + timedelta(days=5))


def heizung() -> list[Shipment]:
    """Bestellt (Blockindex), Versendet (shipmentId), Geliefert (nur Bestellnummer)."""
    return [
        _amazon("1", ShipmentStatus.ORDERED, days=0),
        _amazon("t3vknf4py", ShipmentStatus.IN_TRANSIT, days=1.6),
        _amazon("", ShipmentStatus.DELIVERED, days=3.4),
    ]


# ----------------------------------------------------------------------
# Regel
# ----------------------------------------------------------------------


def test_paketkennung_aus_der_id() -> None:
    """shipmentId zaehlt, Blockindex und reine Bestellnummer nicht."""
    ordered, shipped, delivered = heizung()

    assert package_anchor(shipped) == "t3vknf4py"
    assert package_anchor(ordered) is None
    assert package_anchor(delivered) is None


def test_zustellung_ohne_kennung_findet_die_aelteste_gleiche_sendung() -> None:
    """Gleiche Artikel: Ziel ist die aelteste Sendung der Bestellung."""
    ordered, shipped, delivered = heizung()

    assert merge_target(delivered, [ordered, shipped]) == ordered


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        # Andere Bestellung.
        (_amazon("t1", ShipmentStatus.IN_TRANSIT, days=1, order="304-0000000-0000000"), None),
        # Anderer Artikel.
        (_amazon("t1", ShipmentStatus.IN_TRANSIT, days=1, items=("Magnete",)), None),
        # Sammelpaket: die Heizung ist Teil davon.
        (
            _amazon("t1", ShipmentStatus.IN_TRANSIT, days=1, items=(HEIZUNG, "Magnete")),
            "amazon_order_304_1496979_1480346_t1",
        ),
    ],
)
def test_kriterium_bestellnummer_und_artikelmenge(
    candidate: Shipment, expected: str | None
) -> None:
    """Bestellnummer allein reicht nie; die Artikel muessen enthalten sein."""
    delivered = _amazon("", ShipmentStatus.DELIVERED, days=3)

    target = merge_target(delivered, [candidate])

    assert (target.id if target else None) == expected


def test_zwei_pakete_mit_gleichem_artikel_bleiben_getrennt() -> None:
    """Menge 2 in zwei Paketen: verschiedene Kennungen, keine Verschmelzung."""
    first = _amazon("ta", ShipmentStatus.IN_TRANSIT, days=1)
    second = _amazon("tb", ShipmentStatus.IN_TRANSIT, days=1)
    delivered = _amazon("", ShipmentStatus.DELIVERED, days=3)

    assert merge_target(second, [first]) is None
    # Welches der beiden Pakete zugestellt wurde, ist nicht zu sagen.
    assert merge_target(delivered, [first, second]) is None


def test_ohne_artikel_kein_kriterium() -> None:
    """Eine Mail ohne Artikel wird nicht geraten zugeordnet."""
    delivered = _amazon("", ShipmentStatus.DELIVERED, days=3, items=())

    assert merge_target(delivered, heizung()[:2]) is None


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------


def test_bestand_wird_zu_einer_zugestellten_sendung(hass: HomeAssistant) -> None:
    """Die drei Familien der Heizung werden eine Sendung mit der aeltesten ID."""
    store = ShipmentStore(hass)
    for shipment in heizung():
        store.async_upsert(shipment)

    merged = store.async_consolidate()

    (only,) = store.shipments
    assert only.id == "amazon_order_304_1496979_1480346_1"
    assert only.status is ShipmentStatus.DELIVERED
    assert {absorbed for absorbed, _, _ in merged} == {
        "amazon_order_304_1496979_1480346",
        "amazon_order_304_1496979_1480346_t3vknf4py",
    }
    assert store.resolve("amazon_order_304_1496979_1480346_t3vknf4py") == only.id
    assert len(only.sources) == 3


def test_neue_mail_landet_direkt_bei_der_bestehenden_sendung(hass: HomeAssistant) -> None:
    """Die Geliefert-Mail wird nicht erst als eigene Sendung angelegt."""
    store = ShipmentStore(hass)
    _, shipped, delivered = heizung()
    store.async_upsert(shipped)

    routed = store.async_route(delivered)

    assert routed.id == shipped.id
    # Spaetere Mails mit derselben ID folgen dem Alias.
    assert store.async_route(delivered).id == shipped.id


def test_alias_verschwindet_mit_der_sendung(hass: HomeAssistant) -> None:
    """Geloescht heisst geloescht — eine spaetere Mail legt neu an."""
    store = ShipmentStore(hass)
    _, shipped, delivered = heizung()
    store.async_upsert(shipped)
    store.async_route(delivered)

    store.async_remove(shipped.id)

    assert store.resolve(delivered.id) == delivered.id


async def test_aliase_ueberleben_neustart(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Die Zuordnung steht im Store."""
    store = ShipmentStore(hass)
    for shipment in heizung():
        store.async_upsert(shipment)
    store.async_consolidate()
    await store.async_save()

    reloaded = ShipmentStore(hass)
    await reloaded.async_load()

    assert reloaded.resolve("amazon_order_304_1496979_1480346") == (
        "amazon_order_304_1496979_1480346_1"
    )


async def test_zusammenlegen_meldet_die_zustellung(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Nach dem ersten Durchlauf ist die Heizung zugestellt — mit Event."""
    events = async_capture_events(hass, EVENT_SHIPMENT_DELIVERED)
    entry = await setup_with(hass, hass_storage, heizung())
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    (only,) = entry.runtime_data.store.shipments
    assert only.status is ShipmentStatus.DELIVERED
    # Genau einmal gemeldet, obwohl zwei Durchlaeufe liefen.
    assert [event.data["id"] for event in events] == [only.id]
    assert entry.runtime_data.store.resolve("amazon_order_304_1496979_1480346") == only.id


# ----------------------------------------------------------------------
# Plugin
# ----------------------------------------------------------------------


def test_sammelbestaetigung_nimmt_die_bestellnummer_je_block(
    email_fixture: Callable[[str], bytes],
) -> None:
    """Eine Bestaetigung ueber zwei Bestellungen: jedes Paket mit seiner eigenen Nummer."""
    raw = email_fixture("amazon/bestellbestaetigung.eml")
    # Ab dem zweiten Paketblock eine andere Bestellnummer.
    first = raw.find(b"303-1234567-1234567", raw.find(b"Ankunft morgen"))
    assert first > 0
    raw = raw[:first] + raw[first:].replace(b"303-1234567-1234567", b"304-7654321-7654321")

    shipments = PLUGIN.parse_all(parse_mail(raw))

    assert [shipment.order_number for shipment in shipments] == [
        "303-1234567-1234567",
        "304-7654321-7654321",
    ]
    assert shipments[1].id.startswith("amazon_order_304_7654321_7654321")


def test_in_zustellung_im_betreff(email_fixture: Callable[[str], bytes]) -> None:
    """„In Zustellung: …" im Betreff heisst In Zustellung — die Balkenzeile nicht."""
    mail = parse_mail(email_fixture("amazon/versandbestaetigung.eml"))
    out = replace(mail, subject="In Zustellung: „Kabelloses Ladegerät...“")

    (shipment,) = PLUGIN.parse_all(out)
    (shipped,) = PLUGIN.parse_all(mail)

    assert shipment.status is ShipmentStatus.OUT_FOR_DELIVERY
    # Der Fortschrittsbalken („In Zustellung" als eigene Zeile) allein aendert nichts.
    assert shipped.status is ShipmentStatus.IN_TRANSIT
