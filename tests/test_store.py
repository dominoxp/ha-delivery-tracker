"""Tests fuer Persistenz und Retention."""

from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.delivery_tracker.const import SOURCE_IMAP, STORAGE_KEY
from custom_components.delivery_tracker.models import (
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
    SourceRecord,
)
from custom_components.delivery_tracker.store import ShipmentStore

NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)


def _shipment(
    shipment_id: str = "dhl_jjd00998899",
    *,
    status: ShipmentStatus = ShipmentStatus.IN_TRANSIT,
    last_updated: datetime = NOW,
    delivered_at: datetime | None = None,
    **overrides: Any,
) -> Shipment:
    """Eine Sendung fuer die Store-Tests."""
    return Shipment(
        id=shipment_id,
        carrier="dhl",
        tracking_number="JJD00998899",
        status=status,
        delivered_at=delivered_at,
        sources=[SourceRecord(source_id=SOURCE_IMAP, fetched_at=last_updated)],
        first_seen=last_updated,
        last_updated=last_updated,
        **overrides,
    )


async def test_upsert_legt_an_und_meldet_statuswechsel(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Neue Sendungen landen im Store, der Statuswechsel wird gemeldet."""
    store = ShipmentStore(hass)
    await store.async_load()

    stored, changed = store.async_upsert(_shipment())

    assert changed is True
    assert stored.status is ShipmentStatus.IN_TRANSIT
    assert store.get("dhl_jjd00998899") is stored
    assert [item.id for item in store.shipments] == ["dhl_jjd00998899"]


async def test_upsert_fuehrt_mit_bestehendem_stand_zusammen(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Ein zweiter Treffer derselben Sendung wird gemergt statt ersetzt."""
    store = ShipmentStore(hass)
    await store.async_load()
    store.async_upsert(
        _shipment(
            sender="Amazon",
            events=[ShipmentEvent(timestamp=NOW, status=ShipmentStatus.IN_TRANSIT)],
        )
    )

    stored, changed = store.async_upsert(
        _shipment(
            status=ShipmentStatus.DELIVERED,
            delivered_at=NOW + timedelta(hours=3),
            last_updated=NOW + timedelta(hours=3),
            events=[
                ShipmentEvent(timestamp=NOW + timedelta(hours=3), status=ShipmentStatus.DELIVERED)
            ],
        )
    )

    assert changed is True
    assert stored.status is ShipmentStatus.DELIVERED
    assert stored.sender == "Amazon"
    assert len(stored.events) == 2
    assert len(store.shipments) == 1


async def test_upsert_ohne_statuswechsel(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Dieselbe Mail zweimal erzeugt keinen Statuswechsel."""
    store = ShipmentStore(hass)
    await store.async_load()
    store.async_upsert(_shipment())

    _, changed = store.async_upsert(_shipment())

    assert changed is False


async def test_persistenz_ueberlebt_neustart(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Geschriebene Sendungen sind nach einem HA-Neustart wieder da."""
    store = ShipmentStore(hass)
    await store.async_load()
    store.async_upsert(
        _shipment(
            sender="Amazon",
            description="Kaffeebohnen 1kg",
            events=[
                ShipmentEvent(
                    timestamp=NOW, status=ShipmentStatus.IN_TRANSIT, text="Im Paketzentrum"
                )
            ],
        )
    )
    await store.async_save()

    assert STORAGE_KEY in hass_storage

    neustart = ShipmentStore(hass)
    await neustart.async_load()

    restored = neustart.get("dhl_jjd00998899")
    assert restored is not None
    assert restored.sender == "Amazon"
    assert restored.description == "Kaffeebohnen 1kg"
    assert restored.events[0].text == "Im Paketzentrum"
    assert restored.last_updated == NOW


async def test_leerer_store_startet_ohne_sendungen(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Ohne gespeicherte Datei startet der Store leer."""
    store = ShipmentStore(hass)
    await store.async_load()

    assert store.shipments == []


async def test_unlesbare_eintraege_werden_verworfen(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Kaputte Daten legen den Start nicht lahm."""
    gute = _shipment()
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {"shipments": [{"carrier": "dhl"}, gute.to_dict()]},
    }

    store = ShipmentStore(hass)
    await store.async_load()

    assert [item.id for item in store.shipments] == [gute.id]


async def test_retention_entfernt_nur_alte_abgeschlossene(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Zugestellt und alt verschwindet, aktiv oder frisch bleibt."""
    store = ShipmentStore(hass, retention_days=14)
    await store.async_load()
    alt = NOW - timedelta(days=20)

    store.async_upsert(
        _shipment("dhl_alt", status=ShipmentStatus.DELIVERED, delivered_at=alt, last_updated=alt)
    )
    store.async_upsert(_shipment("dhl_aktiv", last_updated=alt))
    store.async_upsert(
        _shipment(
            "dhl_frisch",
            status=ShipmentStatus.DELIVERED,
            delivered_at=NOW - timedelta(days=2),
            last_updated=NOW - timedelta(days=2),
        )
    )

    removed = store.async_purge(now=NOW)

    assert removed == ["dhl_alt"]
    assert sorted(item.id for item in store.shipments) == ["dhl_aktiv", "dhl_frisch"]


async def test_retention_null_behaelt_alles(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Retention 0 schaltet die Bereinigung ab."""
    store = ShipmentStore(hass, retention_days=0)
    await store.async_load()
    alt = NOW - timedelta(days=400)
    store.async_upsert(
        _shipment("dhl_alt", status=ShipmentStatus.DELIVERED, delivered_at=alt, last_updated=alt)
    )

    assert store.async_purge(now=NOW) == []
    assert len(store.shipments) == 1


async def test_remove(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Einzelne Sendungen lassen sich loeschen."""
    store = ShipmentStore(hass)
    await store.async_load()
    store.async_upsert(_shipment())

    assert store.async_remove("dhl_jjd00998899") is True
    assert store.async_remove("dhl_jjd00998899") is False
    assert store.shipments == []


async def test_active_shipments(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Die aktive Teilmenge blendet abgeschlossene Sendungen aus."""
    store = ShipmentStore(hass)
    await store.async_load()
    store.async_upsert(_shipment("dhl_aktiv"))
    store.async_upsert(_shipment("dhl_zu", status=ShipmentStatus.DELIVERED, delivered_at=NOW))

    assert [item.id for item in store.active_shipments] == ["dhl_aktiv"]


async def test_stale_days_entfernt_aktive_ohne_aktualisierung(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Aktiv und seit `stale_days` still verschwindet; lange laufend mit frischer Mail bleibt."""
    store = ShipmentStore(hass, retention_days=14, stale_days=60)
    await store.async_load()
    store.async_upsert(
        _shipment("amazon_order_vergessen", status=ShipmentStatus.ORDERED, last_updated=NOW)
    )
    # Ueber `upsert` laesst sich `last_updated` nicht zuruecksetzen; die alte
    # Sendung entsteht daher direkt mit altem Zeitstempel.
    store.async_upsert(
        _shipment(
            "amazon_order_alt",
            status=ShipmentStatus.ORDERED,
            last_updated=NOW - timedelta(days=61),
        )
    )
    lange_unterwegs = _shipment("dhl_lang", last_updated=NOW - timedelta(days=1))
    lange_unterwegs.first_seen = NOW - timedelta(days=90)
    store.async_upsert(lange_unterwegs)

    removed = store.async_purge(now=NOW)

    assert removed == ["amazon_order_alt"]
    assert sorted(item.id for item in store.shipments) == ["amazon_order_vergessen", "dhl_lang"]


async def test_stale_days_null_schaltet_ab(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """`stale_days = 0` laesst aktive Sendungen unbegrenzt stehen."""
    store = ShipmentStore(hass, stale_days=0)
    await store.async_load()
    store.async_upsert(_shipment("dhl_uralt", last_updated=NOW - timedelta(days=400)))

    assert store.async_purge(now=NOW) == []


async def test_quellen_zustand_ueberlebt_neustart(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Der Fortschritt einer Quelle wird mitgespeichert und laesst sich aufraeumen."""
    store = ShipmentStore(hass)
    await store.async_load()
    store.async_set_source_state("postfach_1", {"uid_validity": 7, "last_uid": 42})
    store.async_set_source_state("geloescht", {"last_uid": 1})
    store.async_prune_source_states({"postfach_1"})
    await store.async_save()

    neustart = ShipmentStore(hass)
    await neustart.async_load()

    assert neustart.source_state("postfach_1") == {"uid_validity": 7, "last_uid": 42}
    assert neustart.source_state("geloescht") is None
