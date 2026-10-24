"""Tests der Sendungsverwaltung im Options-Flow.

Die Sendungen liegen im Store (`hass_storage`), wie sie ein frueherer Lauf
gespeichert haette. Die Uhr steht fest, damit Alter und Verfall eindeutig sind.
"""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delivery_tracker.addresses import suggest_destination
from custom_components.delivery_tracker.carriers import async_get_registry
from custom_components.delivery_tracker.const import (
    DOMAIN,
    SOURCE_IMAP,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_DESTINATION,
    SUBENTRY_MAILBOX,
)
from custom_components.delivery_tracker.merge import MAX_SOURCE_RECORDS, merge_sources
from custom_components.delivery_tracker.models import (
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
    SourceRecord,
)
from custom_components.delivery_tracker.pipeline import (
    MAIL_LOG_SIZE,
    MailLogEntry,
    MailOutcome,
    MailPipeline,
)
from custom_components.delivery_tracker.sources import ImapSource
from custom_components.delivery_tracker.store import ExpiryRule, ShipmentStore
from tests.test_config_flow import count_reloads, start_options_step

# Dienstag, 6. Oktober 2026, 12:00 in Berlin.
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=dt_util.UTC)

HOME = ConfigSubentryData(
    data={"id": "home", "name": "Zuhause", "postal_codes": ["10115"]},
    subentry_type=SUBENTRY_DESTINATION,
    title="Zuhause",
    unique_id="home",
)


@pytest.fixture(autouse=True)
async def berlin(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Zeitzone Berlin, deutsche Oberflaeche, Uhr fest."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    hass.config.language = "de"
    freezer.move_to(NOW)


def _shipment(
    shipment_id: str,
    status: ShipmentStatus = ShipmentStatus.IN_TRANSIT,
    *,
    days_ago: int = 0,
    destination_raw: str | None = None,
    pickup_location: str | None = None,
) -> Shipment:
    when = NOW - timedelta(days=days_ago)
    return Shipment(
        id=shipment_id,
        carrier="amazon",
        tracking_number=shipment_id.upper(),
        status=status,
        description=f"Artikel {shipment_id}",
        delivered_at=when if status is ShipmentStatus.DELIVERED else None,
        destination_raw=destination_raw,
        pickup_location=pickup_location,
        events=[ShipmentEvent(timestamp=when, status=status, text="Paket | unterwegs")],
        sources=[
            SourceRecord(
                source_id=SOURCE_IMAP,
                fetched_at=when,
                reference=f"<{shipment_id}@amazon.de>",
                subject=f"Versandbestätigung {shipment_id}",
                sender="Amazon <versand@amazon.de>",
            )
        ],
        first_seen=when,
        last_updated=when,
    )


def beispieldaten() -> list[Shipment]:
    """Eine frische, eine alte offene, eine zugestellte Sendung an einen Paketshop."""
    return [
        _shipment("frisch", days_ago=1, destination_raw="Max Muster, Hauptstr. 1, 10115 Berlin"),
        _shipment("alt", days_ago=37, destination_raw="Kiosk Ecke, Nebenweg 5, 12345 Dorf"),
        _shipment(
            "shop",
            ShipmentStatus.READY_FOR_PICKUP,
            days_ago=3,
            pickup_location="Paketshop 4711, Nebenweg 5, 12345 Dorf",
        ),
    ]


async def setup_with(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    shipments: list[Shipment],
    *subentries: ConfigSubentryData,
) -> MockConfigEntry:
    """Entry mit gespeicherten Sendungen laden."""
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {"shipments": [shipment.to_dict() for shipment in shipments]},
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Delivery Tracker",
        data={},
        options={"retention_days": 14, "stale_days": 60},
        subentries_data=list(subentries),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def offered(result: Any, field: str) -> list[dict[str, str]]:
    """Die Optionen einer Auswahlliste im Formular."""
    return result["data_schema"].schema[field].config["options"]


async def configure(hass: HomeAssistant, result: Any, user_input: dict[str, Any]) -> Any:
    """Einen Schritt ausfuellen und auf Folgen warten."""
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


# ----------------------------------------------------------------------
# Menue und Liste
# ----------------------------------------------------------------------


async def test_menue_bietet_einstellungen_sendungen_und_mails(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Das Options-Menue fuehrt zu allen drei Bereichen."""
    entry = await setup_with(hass, hass_storage, [])
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert list(result["menu_options"]) == ["settings", "shipments", "mails"]


async def test_liste_zeigt_alter_und_verfall_aelteste_zuerst(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Jede Zeile nennt Zusteller, Status, Artikel, Zielort, Alter und Verfall."""
    entry = await setup_with(hass, hass_storage, beispieldaten(), HOME)
    result = await start_options_step(hass, entry, "shipments")

    assert result["type"] is FlowResultType.FORM
    options = offered(result, "shipments")
    assert [option["value"] for option in options] == ["alt", "shop", "frisch"]
    alt = options[0]["label"]
    assert "Unterwegs" in alt
    assert "„Artikel alt“" in alt
    assert "→ Unbekannt" in alt
    assert "letzte Mail vor 37 Tagen" in alt
    assert "verschwindet in 23 Tagen" in alt
    assert "→ Zuhause" in options[2]["label"]
    assert "**3 Sendungen**, davon 2 ohne Zielort" in result["description_placeholders"]["overview"]


async def test_leere_liste_bricht_mit_hinweis_ab(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Ohne Sendungen gibt es nichts zu verwalten."""
    entry = await setup_with(hass, hass_storage, [])
    result = await start_options_step(hass, entry, "shipments")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_shipments"


async def test_loeschen_braucht_bestaetigung_und_aendert_keine_optionen(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Ohne Haken bleibt alles stehen; mit Haken verschwinden die Sendungen ohne Reload."""
    entry = await setup_with(hass, hass_storage, beispieldaten())
    options_before = dict(entry.options)
    result = await start_options_step(hass, entry, "shipments")

    result = await configure(hass, result, {"shipments": ["alt", "shop"], "action": "delete"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "confirm_required"}
    assert entry.runtime_data.store.get("alt") is not None

    with count_reloads(hass) as reloads:
        result = await configure(
            hass, result, {"shipments": ["alt", "shop"], "action": "delete", "confirm": True}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "shipments_removed"
    assert result["description_placeholders"] == {"count": "2"}
    assert [shipment.id for shipment in entry.runtime_data.store.shipments] == ["frisch"]
    assert entry.options == options_before
    assert reloads.call_count == 0
    assert entry.runtime_data.coordinator.data.removed == ["alt", "shop"]


async def test_als_zugestellt_markieren(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Markierte Sendungen sind danach zugestellt und laufen in die Aufbewahrung."""
    entry = await setup_with(hass, hass_storage, beispieldaten())
    result = await start_options_step(hass, entry, "shipments")

    result = await configure(hass, result, {"shipments": ["alt"], "action": "mark_delivered"})

    assert result["reason"] == "shipments_delivered"
    shipment = entry.runtime_data.store.get("alt")
    assert shipment is not None
    assert shipment.status is ShipmentStatus.DELIVERED
    assert entry.runtime_data.store.expiry(shipment).rule is ExpiryRule.CLOSED


@pytest.mark.parametrize(
    ("selected", "error"), [([], "select_none"), (["alt", "shop"], "select_one")]
)
async def test_details_brauchen_genau_eine_sendung(
    hass: HomeAssistant, hass_storage: dict[str, Any], selected: list[str], error: str
) -> None:
    """Keine oder mehrere markiert: Hinweis statt Details."""
    entry = await setup_with(hass, hass_storage, beispieldaten())
    result = await start_options_step(hass, entry, "shipments")

    result = await configure(hass, result, {"shipments": selected, "action": "details"})

    assert result["errors"] == {"base": error}


# ----------------------------------------------------------------------
# Details
# ----------------------------------------------------------------------


async def open_details(hass: HomeAssistant, entry: MockConfigEntry, shipment_id: str) -> Any:
    """Die Detailansicht einer Sendung oeffnen."""
    result = await start_options_step(hass, entry, "shipments")
    return await configure(hass, result, {"shipments": [shipment_id], "action": "details"})


async def test_details_zeigen_verfall_verlauf_und_mails(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Warum noch da, alle Statuswechsel, Betreff und Absender jeder Mail."""
    entry = await setup_with(hass, hass_storage, beispieldaten())
    result = await open_details(hass, entry, "alt")

    assert result["step_id"] == "shipment"
    details = result["description_placeholders"]
    assert details["title"] == "Artikel alt"
    assert "Nicht zugestellt, letzte Mail am 30.08.2026." in details["expiry"]
    assert "Wird am 29.10.2026 automatisch entfernt" in details["expiry"]
    # Pipe im Text darf die Tabelle nicht zerlegen.
    assert "Paket \\| unterwegs" in details["events"]
    assert "Versandbestätigung alt" in details["sources"]
    assert "Amazon \\<versand@amazon.de\\>" in details["sources"]
    assert "**Erkannte Lieferadresse:** Kiosk Ecke, Nebenweg 5, 12345 Dorf" in details["facts"]


async def test_details_zurueck_zur_liste_und_loeschen(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """„Zurück" zeigt die Liste, Löschen aus den Details braucht ebenfalls den Haken."""
    entry = await setup_with(hass, hass_storage, beispieldaten())
    result = await open_details(hass, entry, "alt")

    result = await configure(hass, result, {"action": "back"})
    assert result["step_id"] == "shipments"

    result = await configure(hass, result, {"shipments": ["alt"], "action": "details"})
    result = await configure(hass, result, {"action": "delete"})
    assert result["errors"] == {"base": "confirm_required"}
    result = await configure(hass, result, {"action": "delete", "confirm": True})
    assert result["reason"] == "shipments_removed"
    assert entry.runtime_data.store.get("alt") is None


# ----------------------------------------------------------------------
# Zielort aus Sendung
# ----------------------------------------------------------------------


async def test_zielort_aus_abholort_vorbefuellt_und_sendung_fest_zugeordnet(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Paketshop vergessen: Formular aus dem Abholort, Sendung danach beim neuen Zielort."""
    entry = await setup_with(hass, hass_storage, beispieldaten(), HOME)
    result = await open_details(hass, entry, "shop")
    actions = list(offered(result, "action"))
    assert "destination_new" in actions
    assert "destination_extend" in actions

    result = await configure(hass, result, {"action": "destination_new"})
    assert result["step_id"] == "destination"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested["name"] == "Paketshop 4711"
    assert suggested["packstations"] == ["4711"]
    assert suggested["streets"] == ["Nebenweg 5"]
    assert suggested["postal_codes"] == ["12345"]
    assert suggested["cities"] == ["Dorf"]

    result = await configure(
        hass,
        result,
        {"name": "Paketshop Ecke", "streets": ["Nebenweg 5"], "postal_codes": ["12345"]},
    )
    assert result["step_id"] == "destination_confirm"
    # Die Regeln passen auf „alt" (gleiche Adresse); „shop" ist die geoeffnete Sendung.
    assert result["description_placeholders"]["matched"] == "1"
    default = next(key.default() for key in result["data_schema"].schema if str(key) == "assign")
    assert default == ["shop", "alt"]

    result = await configure(hass, result, {"assign": ["shop", "alt"]})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "destination_saved"
    assert result["description_placeholders"] == {"name": "Paketshop Ecke", "count": "2"}

    assert entry.state is ConfigEntryState.LOADED
    titles = [item.title for item in entry.get_subentries_of_type(SUBENTRY_DESTINATION)]
    assert titles == ["Zuhause", "Paketshop Ecke"]
    store = entry.runtime_data.store
    assert store.get("shop").destination_id == "paketshop_ecke"
    assert store.get("alt").destination_id == "paketshop_ecke"
    assert store.get("frisch").destination_id == "home"


async def test_zielort_name_schon_vergeben(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Gleiche Pruefung wie beim normalen Anlegen eines Zielorts."""
    entry = await setup_with(hass, hass_storage, beispieldaten(), HOME)
    result = await open_details(hass, entry, "alt")
    result = await configure(hass, result, {"action": "destination_new"})

    result = await configure(hass, result, {"name": "zuhause", "cities": ["Dorf"]})

    assert result["errors"] == {"name": "name_exists"}


async def test_adresse_zu_bestehendem_zielort_hinzufuegen(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Die neuen Regeln kommen zu den alten dazu."""
    entry = await setup_with(hass, hass_storage, beispieldaten(), HOME)
    (home,) = entry.get_subentries_of_type(SUBENTRY_DESTINATION)
    result = await open_details(hass, entry, "alt")
    result = await configure(hass, result, {"action": "destination_extend"})
    assert result["step_id"] == "destination_extend"

    result = await configure(hass, result, {"target": home.subentry_id, "postal_codes": ["12345"]})
    result = await configure(hass, result, {"assign": ["alt"]})

    assert result["reason"] == "destination_saved"
    data = entry.subentries[home.subentry_id].data
    assert data["postal_codes"] == ["10115", "12345"]
    assert entry.runtime_data.store.get("alt").destination_id == "home"


# ----------------------------------------------------------------------
# Letzte Mails
# ----------------------------------------------------------------------


async def test_mails_ohne_postfach(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Ohne Postfach gibt es kein Protokoll."""
    entry = await setup_with(hass, hass_storage, [])
    result = await start_options_step(hass, entry, "mails")

    assert result["reason"] == "no_mailboxes"


async def test_mails_zeigen_nicht_erkannte_und_auf_wunsch_alle(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Standard: nur Problemfaelle; „Alle" zeigt auch erkannte Mails."""
    mailbox = ConfigSubentryData(
        data={
            "server": "127.0.0.1",
            "port": 1,
            "security": "none",
            "username": "u",
            "password": "p",
            "folder": "INBOX",
        },
        subentry_type=SUBENTRY_MAILBOX,
        title="Privat",
        unique_id="u@127.0.0.1",
    )
    # Das Postfach soll sich nicht verbinden; es geht nur um sein Protokoll.
    with (
        patch.object(ImapSource, "async_start", AsyncMock()),
        patch.object(ImapSource, "async_fetch", AsyncMock(return_value=[])),
    ):
        entry = await setup_with(hass, hass_storage, [], mailbox)
    (handle,) = entry.runtime_data.sources.values()
    pipeline = handle.pipeline
    assert pipeline is not None
    pipeline._log.extend(
        [
            MailLogEntry(
                processed_at=NOW,
                outcome=MailOutcome.MATCHED,
                subject="Ihr Paket ist unterwegs",
                carrier="dhl",
                shipments=(("dhl_1", "in_transit"),),
            ),
            MailLogEntry(
                processed_at=NOW,
                outcome=MailOutcome.WITHOUT_SHIPMENT,
                subject="Zugestellt: Kaffeemaschine",
                sender="Amazon <versand@amazon.de>",
                carrier="amazon",
            ),
        ]
    )

    result = await start_options_step(hass, entry, "mails")
    mails = result["description_placeholders"]["mails"]
    assert "### Privat" in mails
    assert "Zugestellt: Kaffeemaschine" in mails
    assert "⚠️ Keine Sendung · Amazon" in mails
    assert "Ihr Paket ist unterwegs" not in mails

    result = await configure(hass, result, {"filter": "all"})
    mails = result["description_placeholders"]["mails"]
    assert "✅ Sendung erkannt · DHL Paket · Unterwegs" in mails
    # Neueste zuerst.
    assert mails.index("Zugestellt") < mails.index("Ihr Paket")


# ----------------------------------------------------------------------
# Bausteine
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "days_ago", "stale", "retention", "rule", "expected"),
    [
        (ShipmentStatus.IN_TRANSIT, 10, 60, 14, ExpiryRule.STALE, 50),
        (ShipmentStatus.DELIVERED, 10, 60, 14, ExpiryRule.CLOSED, 4),
        (ShipmentStatus.IN_TRANSIT, 10, 0, 14, ExpiryRule.STALE, None),
        (ShipmentStatus.DELIVERED, 10, 60, 0, ExpiryRule.CLOSED, None),
    ],
)
def test_expiry_regeln(
    hass: HomeAssistant,
    status: ShipmentStatus,
    days_ago: int,
    stale: int,
    retention: int,
    rule: ExpiryRule,
    expected: int | None,
) -> None:
    """Verfall je Regel; 0 schaltet ab."""
    store = ShipmentStore(hass, retention_days=retention, stale_days=stale)
    expiry = store.expiry(_shipment("x", status, days_ago=days_ago))

    assert expiry.rule is rule
    if expected is None:
        assert expiry.at is None
    else:
        assert expiry.at == NOW + timedelta(days=expected)


def test_purge_entfernt_genau_die_faelligen(hass: HomeAssistant) -> None:
    """Purge und Anzeige nutzen dieselbe Regel."""
    store = ShipmentStore(hass, retention_days=14, stale_days=30)
    for shipment in (
        _shipment("offen_alt", days_ago=31),
        _shipment("offen_neu", days_ago=29),
        _shipment("zu_alt", ShipmentStatus.DELIVERED, days_ago=15),
        _shipment("zu_neu", ShipmentStatus.DELIVERED, days_ago=13),
    ):
        store.async_upsert(shipment)

    assert sorted(store.async_purge(now=NOW)) == ["offen_alt", "zu_alt"]
    assert all(not store.expiry(item).is_due(NOW) for item in store.shipments)


@pytest.mark.parametrize(
    ("text", "name", "rules"),
    [
        (
            "Packstation 123, 12345 Berlin",
            "Packstation 123",
            {"packstations": ["123"], "postal_codes": ["12345"], "cities": ["Berlin"]},
        ),
        (
            "Max Muster, Hauptstraße 12a, 10115 Berlin",
            "Max Muster",
            {"streets": ["Hauptstraße 12a"], "postal_codes": ["10115"], "cities": ["Berlin"]},
        ),
        ("Postnummer 12345678", "", {"post_numbers": ["12345678"]}),
        (
            # So schreibt Amazon einen Locker: Striche statt Kommas, PLZ ohne Ort.
            "Amazon Locker - Lindenhof MUSTERKIOSK - Alt-Musterstadt - "
            "Musterweg 7 Musterstraße 7 HE , 99999",
            "Amazon Locker Lindenhof MUSTERKIOSK",
            {
                "streets": ["Musterweg 7", "Musterstraße 7"],
                "postal_codes": ["99999"],
                "keywords": ["Lindenhof MUSTERKIOSK", "Alt-Musterstadt"],
            },
        ),
        ("Erika \u2013 Alt-musterstadt", "Erika", {"keywords": ["Alt-musterstadt"]}),
        # HTML-Reste aus der Mail stoeren nicht.
        ("Paketshop 785</a><br />", "Paketshop 785", {"packstations": ["785"]}),
        (None, "", {}),
    ],
)
def test_vorschlag_aus_adresse(text: str | None, name: str, rules: dict[str, list[str]]) -> None:
    """Die Adresszeile wird grob in Regeln zerlegt; der Rest wird nur Name."""
    suggestion = suggest_destination(text)

    assert suggestion.name == name
    assert {key: value for key, value in suggestion.rules.items() if value} == rules


def test_merge_behaelt_jede_mail(hass: HomeAssistant) -> None:
    """Je Mail ein Nachweis, gedeckelt, juengste behalten."""
    records = [
        SourceRecord(
            source_id=SOURCE_IMAP,
            fetched_at=NOW + timedelta(minutes=index),
            reference=f"<{index}@x>",
            subject=f"Mail {index}",
        )
        for index in range(MAX_SOURCE_RECORDS + 5)
    ]
    merged = merge_sources(records[:3], records[3:])

    assert len(merged) == MAX_SOURCE_RECORDS
    assert merged[-1].subject == f"Mail {MAX_SOURCE_RECORDS + 4}"
    assert merged[0].subject == "Mail 5"
    # Dieselbe Mail zweimal ergibt einen Eintrag.
    assert len(merge_sources(records[:1], records[:1])) == 1


def test_source_record_roundtrip_und_alte_daten() -> None:
    """Betreff und Absender werden gespeichert; alte Eintraege laden ohne."""
    record = SourceRecord(
        source_id=SOURCE_IMAP, fetched_at=NOW, reference="<a@b>", subject="S", sender="A <a@b>"
    )
    assert SourceRecord.from_dict(record.to_dict()) == record

    old = record.to_dict()
    del old["subject"], old["sender"]
    restored = SourceRecord.from_dict(old)
    assert restored.subject is None
    assert restored.sender is None


async def test_pipeline_protokolliert_jede_mail(hass: HomeAssistant, email_fixture: Any) -> None:
    """Erkannt, ohne Sendung, unbekannt, kaputt — und gedeckelt auf MAIL_LOG_SIZE."""
    registry = await async_get_registry(hass)
    pipeline = MailPipeline(registry)

    pipeline.process(email_fixture("dhl/sendungsankuendigung.eml"), uid=1)
    pipeline.process(
        b"From: Jemand <a@example.org>\r\nSubject: Hallo\r\nMessage-ID: <x@y>\r\n\r\nText", uid=2
    )

    first, second = pipeline.log
    assert first.outcome is MailOutcome.MATCHED
    assert first.carrier == "dhl"
    assert first.shipments
    assert first.subject
    assert second.outcome is MailOutcome.UNMATCHED
    assert second.subject == "Hallo"
    assert second.sender == "Jemand <a@example.org>"
    assert MailLogEntry.from_dict(first.to_dict()) == first

    for _ in range(MAIL_LOG_SIZE):
        pipeline.process(b"Subject: x\r\n\r\ny")
    assert len(pipeline.log) == MAIL_LOG_SIZE


async def test_protokoll_ueberlebt_neustart(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Der Store speichert das Protokoll je Postfach."""
    store = ShipmentStore(hass)
    entry = MailLogEntry(processed_at=NOW, outcome=MailOutcome.UNMATCHED, subject="Hallo")
    store.async_set_mail_log("box", [entry.to_dict()])
    await store.async_save()

    reloaded = ShipmentStore(hass)
    await reloaded.async_load()
    assert [MailLogEntry.from_dict(item) for item in reloaded.mail_log("box")] == [entry]

    reloaded.async_prune_source_states(set())
    assert reloaded.mail_log("box") == []
