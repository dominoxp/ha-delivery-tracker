"""Tests der Zieladressen-Zuordnung.

Drei Zielorte, wie sie ein typischer Haushalt konfiguriert: Wohnung, Buero und
Packstation. Wohnung und Packstation liegen im selben Ort — die Zuordnung muss
sie trotzdem auseinanderhalten.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.delivery_tracker.addresses import (
    AddressMatcher,
    enrich_from_mail,
    extract_destination_raw,
    normalize,
    normalize_email,
)
from custom_components.delivery_tracker.carriers import CarrierRegistry
from custom_components.delivery_tracker.carriers.dhl import PLUGIN as DHL_PLUGIN
from custom_components.delivery_tracker.carriers.example import PLUGIN as EXAMPLE_PLUGIN
from custom_components.delivery_tracker.const import DESTINATION_UNKNOWN, SOURCE_IMAP
from custom_components.delivery_tracker.mail import MailMessage
from custom_components.delivery_tracker.merge import merge_shipments
from custom_components.delivery_tracker.models import Destination, Shipment, SourceRecord
from custom_components.delivery_tracker.pipeline import MailPipeline
from custom_components.delivery_tracker.store import ShipmentStore

Loader = Callable[[str], bytes]

NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)

ZUHAUSE = Destination(
    id="zuhause",
    name="Zuhause",
    recipient_names=["Max Mustermann"],
    streets=["Musterstraße 1"],
    postal_codes=["12345"],
    cities=["Musterstadt"],
    emails=["max.mustermann@example.org"],
)
BUERO = Destination(
    id="buero",
    name="Büro",
    recipient_names=["Max Mustermann"],
    streets=["Firmenweg 7"],
    postal_codes=["54321"],
    cities=["Beispielhausen"],
    keywords=["Beispiel GmbH"],
)
PACKSTATION = Destination(
    id="packstation",
    name="Packstation",
    packstations=["Packstation 123"],
    post_numbers=["12345678"],
)
DREI_ZIELORTE = [ZUHAUSE, BUERO, PACKSTATION]


def _match(
    raw: str | None,
    *,
    emails: tuple[str, ...] = (),
    destinations: list[Destination] = DREI_ZIELORTE,
) -> str:
    """Die ID des Zielorts, den die Zuordnung waehlt."""
    return AddressMatcher(destinations).match(raw, emails).destination_id


def _shipment(shipment_id: str = "dhl_00340434161094042717", **overrides: Any) -> Shipment:
    """Eine Sendung fuer die Zuordnungs-Tests."""
    return Shipment(
        id=shipment_id,
        carrier="dhl",
        sources=[SourceRecord(source_id=SOURCE_IMAP, fetched_at=NOW)],
        first_seen=NOW,
        last_updated=NOW,
        **overrides,
    )


# ----------------------------------------------------------------------
# Akzeptanzkriterien
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Max Mustermann, Musterstraße 1, 12345 Musterstadt", "zuhause"),
        ("Beispiel GmbH, Max Mustermann, Firmenweg 7, 54321 Beispielhausen", "buero"),
        ("Max Mustermann, 12345678, Packstation 123, 12345 Musterstadt", "packstation"),
    ],
)
def test_vollstaendige_adresse_trifft_den_richtigen_von_drei_zielorten(raw: str, expected: str):
    """Die vollstaendige Anschrift ordnet eindeutig zu."""
    assert _match(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12345 Musterstadt", "zuhause"), ("54321 Beispielhausen", "buero")],
)
def test_nur_postleitzahl_und_ort_reichen(raw: str, expected: str):
    """Viele Zusteller kuerzen die Adresse auf PLZ und Ort."""
    assert _match(raw) == expected


def test_einzelne_postleitzahl_reicht():
    """Auch ein einziger Treffer muss zu einer Zuordnung fuehren koennen."""
    assert _match("PLZ 54321") == "buero"


@pytest.mark.parametrize(
    "raw",
    [
        "Postnummer 12345678, Packstation 123",
        "PACKSTATION NR. 123 / 12345678",
        "Max Mustermann\n12345678\nPackstation 123\n12345 Musterstadt",
    ],
)
def test_packstation_allein_ueber_automaten_und_postnummer(raw: str):
    """Packstation-Sendungen tragen keine Strasse — das muss allein reichen."""
    assert _match(raw) == "packstation"


def test_packstation_nummer_ohne_kontext_ist_keine_packstation():
    """Eine 123 in der Hausnummer ist keine Automatennummer."""
    assert _match("Hauptstraße 123, 99999 Nirgendwo") == DESTINATION_UNKNOWN


@pytest.mark.parametrize(
    "raw",
    [
        "Müllerstraße 4a",
        "Muellerstrasse 4a",
        "MUELLERSTR. 4 A",
        "Müller-Str. 4a",
        "Müller Straße 4a",
        "mullerstr 4a",
    ],
)
def test_umlaut_und_abkuerzungsvarianten_fuehren_zur_selben_adresse(raw: str):
    """Schreibweisen derselben Strasse duerfen keinen Unterschied machen."""
    wohnung = Destination(id="wohnung", name="Wohnung", streets=["Müllerstraße 4a"])

    assert _match(raw, destinations=[wohnung]) == "wohnung"


def test_ohne_erkennbare_adresse_bleibt_unbekannt_mit_rohtext():
    """Keine Zuordnung, aber `destination_raw` bleibt fuer eine neue Regel stehen."""
    shipment = _shipment(destination_raw="Irgendwo 5, 99999 Nirgendwo")

    AddressMatcher(DREI_ZIELORTE).assign(shipment)

    assert shipment.destination_id == DESTINATION_UNKNOWN
    assert shipment.destination_raw == "Irgendwo 5, 99999 Nirgendwo"


def test_ohne_jede_adressangabe_bleibt_unbekannt():
    """Weder Adresszeile noch Empfaengeradresse: nichts zu vergleichen."""
    shipment = _shipment()

    AddressMatcher(DREI_ZIELORTE).assign(shipment)

    assert shipment.destination_id == DESTINATION_UNKNOWN
    assert shipment.destination_raw is None


def test_aehnliche_adressen_im_selben_ort_werden_nicht_verwechselt():
    """Gleicher Ort, gleiche PLZ, fast gleiche Strasse — die genauere gewinnt."""
    vorne = Destination(
        id="vorne", name="Vorne", streets=["Musterstraße 1"], postal_codes=["12345"]
    )
    hinten = Destination(
        id="hinten", name="Hinten", streets=["Musterstraße 11"], postal_codes=["12345"]
    )
    neu = Destination(id="neu", name="Neu", streets=["Neue Musterstraße 1"], postal_codes=["12345"])
    # „vorne" steht zuerst: Gewinnen die anderen, liegt es nicht an der Reihenfolge.
    zielorte = [vorne, hinten, neu]

    assert _match("Musterstraße 1, 12345 Musterstadt", destinations=zielorte) == "vorne"
    assert _match("Musterstraße 11, 12345 Musterstadt", destinations=zielorte) == "hinten"
    assert _match("Neue Musterstraße 1, 12345 Musterstadt", destinations=zielorte) == "neu"


async def test_geaenderte_regel_ordnet_beim_naechsten_durchlauf_neu_zu(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Der Nutzer legt eine Regel an — die Sendung zieht um, ohne neue Mail."""
    store = ShipmentStore(hass)
    await store.async_load()
    store.async_upsert(_shipment(destination_raw="Ferienweg 2, 23456 Seebad"))

    assert store.async_assign_destinations(AddressMatcher(DREI_ZIELORTE)) == [
        "dhl_00340434161094042717"
    ]
    assert store.get("dhl_00340434161094042717").destination_id == DESTINATION_UNKNOWN

    ferien = Destination(id="ferien", name="Ferienwohnung", postal_codes=["23456"])
    moved = store.async_assign_destinations(AddressMatcher([*DREI_ZIELORTE, ferien]))

    assert moved == ["dhl_00340434161094042717"]
    stored = store.get("dhl_00340434161094042717")
    assert stored.destination_id == "ferien"
    # Die Neubewertung ist keine Aenderung an der Sendung selbst.
    assert stored.last_updated == NOW
    # Ein zweiter Durchlauf ohne Regelaenderung bewegt nichts.
    assert store.async_assign_destinations(AddressMatcher([*DREI_ZIELORTE, ferien])) == []


# ----------------------------------------------------------------------
# Bewertung im Detail
# ----------------------------------------------------------------------


def test_gleichstand_entscheidet_die_reihenfolge():
    """Zwei gleich gute Zielorte: der zuerst konfigurierte gewinnt."""
    erster = Destination(id="erster", name="Erster", postal_codes=["12345"])
    zweiter = Destination(id="zweiter", name="Zweiter", postal_codes=["12345"])

    assert _match("12345 Musterstadt", destinations=[erster, zweiter]) == "erster"
    assert _match("12345 Musterstadt", destinations=[zweiter, erster]) == "zweiter"


def test_ort_allein_reicht_nicht():
    """Der Ort steht auch in Shop- und Filialnamen — allein ist er zu schwach."""
    assert _match("Musterstadt") == DESTINATION_UNKNOWN


def test_fremde_postleitzahl_schliesst_den_zielort_aus():
    """Name allein trifft Wohnung und Buero; die fremde PLZ verraet: keins von beiden."""
    assert _match("Max Mustermann, Irgendwo 5, 99999 Nirgendwo") == DESTINATION_UNKNOWN
    # Ohne PLZ in der Zeile bleibt der Name ein gueltiger Treffer.
    assert _match("Max Mustermann") == "zuhause"


def test_postleitzahl_mit_leerzeichen():
    """„12 345" in der Mail und „123 45" in der Regel sind dieselbe PLZ."""
    zielort = Destination(id="z", name="Z", postal_codes=["123 45"])

    assert _match("D-12 345 Musterstadt", destinations=[zielort]) == "z"


def test_name_in_anderer_reihenfolge():
    """„Mustermann, Max" ist derselbe Empfaenger wie „Max Mustermann"."""
    assert _match("Mustermann, Max") == "zuhause"


def test_aehnlicher_name_trifft_nicht():
    """„Maxi" ist nicht „Max"."""
    assert _match("Maxi Mustermann") == DESTINATION_UNKNOWN


def test_schluesselwort_trifft():
    """Ein Firmenname in der Adresszeile ordnet dem Buero zu."""
    assert _match("z. Hd. Empfang, BEISPIEL GMBH") == "buero"


@pytest.mark.parametrize(
    ("emails", "expected"),
    [
        (("max.mustermann@example.org",), "zuhause"),
        (("Max.Mustermann+shop@Example.org",), "zuhause"),
        (("jemand.anders@example.org",), DESTINATION_UNKNOWN),
        (("einkauf@firma.example",), "buero"),
    ],
)
def test_empfaenger_mailadresse_auch_als_alias(emails: tuple[str, ...], expected: str):
    """Plus-Adressen gehoeren zur Grundadresse, „@domain" steht fuer alle."""
    buero = Destination(id="buero", name="Büro", emails=["@firma.example"])

    assert _match(None, emails=emails, destinations=[ZUHAUSE, buero]) == expected


def test_treffer_werden_benannt():
    """Fuer Diagnostics: welche Regelarten zur Bewertung beigetragen haben."""
    result = AddressMatcher(DREI_ZIELORTE).match(
        "Max Mustermann, Musterstraße 1, 12345 Musterstadt"
    )

    assert result.destination_id == "zuhause"
    assert result.hits == ("city", "postal_code", "recipient_name", "street")


def test_ohne_zielorte_ist_alles_unbekannt():
    """Vor der ersten Konfiguration landet jede Sendung bei „Unbekannt"."""
    assert _match("Musterstraße 1, 12345 Musterstadt", destinations=[]) == DESTINATION_UNKNOWN


def test_leere_regeln_treffen_nichts():
    """Ein leerer Eintrag aus dem Formular darf nicht jede Sendung fangen."""
    zielort = Destination(id="leer", name="Leer", streets=[""], postal_codes=[" "], keywords=["-"])

    assert _match("Musterstraße 1, 12345 Musterstadt", destinations=[zielort]) == (
        DESTINATION_UNKNOWN
    )


def test_zielort_mit_reservierter_id_wird_ignoriert():
    """Die ID des eingebauten Zielorts ist vergeben."""
    falsch = Destination(id=DESTINATION_UNKNOWN, name="Falsch", postal_codes=["12345"])

    matcher = AddressMatcher([falsch, ZUHAUSE])

    assert [destination.id for destination in matcher.destinations] == ["zuhause"]


# ----------------------------------------------------------------------
# Toleranter Textvergleich
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Musterstraße 1", "musterstr 1"),
        ("Muster-Str. 1 A", "musterstr 1a"),
        ("Marktplatz 3", "marktpl 3"),
        ("Müller", "muller"),
        ("Mueller", "muller"),
        ("Café Zoë", "cafe zo"),
        ("  Viel   Leer\nRaum ", "viel leer raum"),
    ],
)
def test_normalize(value: str, expected: str):
    """Alle Schreibweisen laufen auf dieselbe Form zusammen."""
    assert normalize(value) == expected


def test_normalize_email():
    """Gross-/Kleinschreibung, spitze Klammern und Plus-Zusatz fallen weg."""
    assert normalize_email("<Max+Paket@Example.ORG>") == "max@example.org"
    assert normalize_email("@Firma.example") == "@firma.example"


# ----------------------------------------------------------------------
# Adressextraktion aus dem Mailtext
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Ihre Lieferadresse: Musterstraße 1, 12345 Musterstadt\nDanke",
            "Musterstraße 1, 12345 Musterstadt",
        ),
        (
            "Lieferanschrift\nMax Mustermann\nMusterstraße 1\n12345 Musterstadt\n\nGruss",
            "Max Mustermann, Musterstraße 1, 12345 Musterstadt",
        ),
        (
            "Empfänger:\nMax Mustermann\nPackstation 123\n12345 Musterstadt\n"
            "Sendungsnummer: 00340434161094042717",
            "Max Mustermann, Packstation 123, 12345 Musterstadt",
        ),
        ("Zustellung an: 12345 Musterstadt", "12345 Musterstadt"),
        ("Ship to: Max Mustermann, Musterstadt, DE", "Max Mustermann, Musterstadt, DE"),
    ],
)
def test_adresse_aus_dem_mailtext(text: str, expected: str):
    """Nur hinter einer ausdruecklichen Ueberschrift wird gesucht."""
    assert extract_destination_raw(text) == expected


def test_postnummer_wird_angehaengt():
    """Die Postnummer steht oft getrennt von der Adresse."""
    text = "Empfänger: Max Mustermann, Packstation 123\nIhre Postnummer: 1234 5678"

    assert extract_destination_raw(text) == "Max Mustermann, Packstation 123, Postnummer 12345678"
    assert extract_destination_raw("Postnummer: 12345678") == "Postnummer 12345678"


@pytest.mark.parametrize(
    "text",
    [
        "Der Empfänger war leider nicht anzutreffen.",
        "Zustellung an: Nachbar Familie Beispiel, Musterstraße 3",
        "Abholort: Packstation 123, Musterstraße 5, 12345 Musterstadt",
        "DHL Paket GmbH, Sträßchensweg 10, 53113 Bonn",
    ],
)
def test_keine_adresse_ohne_ueberschrift_und_kein_abholort(text: str):
    """Saetze, Abholorte und das Impressum sind keine Zieladresse."""
    assert extract_destination_raw(text) is None


def test_nachbar_wird_uebersprungen_aber_die_lieferadresse_gefunden():
    """Steht beides in der Mail, zaehlt die eigentliche Lieferadresse."""
    text = "Zustellung an: Nachbar Beispiel\nLieferadresse: Musterstraße 1, 12345 Musterstadt"

    assert extract_destination_raw(text) == "Musterstraße 1, 12345 Musterstadt"


def test_extraktion_ersetzt_keine_plugin_adresse():
    """Die Rueckfallebene greift nur, wenn das Plugin nichts geliefert hat."""
    mail = MailMessage(
        message_id="<x@example.com>",
        text="Lieferadresse: Firmenweg 7, 54321 Beispielhausen",
        to_addresses=("max.mustermann@example.org",),
    )
    vom_plugin = _shipment("a", destination_raw="Musterstraße 1, 12345 Musterstadt")
    ohne = _shipment("b")

    enrich_from_mail([vom_plugin, ohne], mail)

    assert vom_plugin.destination_raw == "Musterstraße 1, 12345 Musterstadt"
    assert ohne.destination_raw == "Firmenweg 7, 54321 Beispielhausen"
    assert ohne.recipient_emails == ["max.mustermann@example.org"]


# ----------------------------------------------------------------------
# Strecke Mail -> Sendung
# ----------------------------------------------------------------------


def test_pipeline_ordnet_zu(email_fixture: Loader):
    """Die Sendung verlaesst die Pipeline bereits mit Zielort."""
    pipeline = MailPipeline(CarrierRegistry([EXAMPLE_PLUGIN]), destinations=DREI_ZIELORTE)

    (shipment,) = pipeline.process(email_fixture("utf8_plain.eml"))

    assert shipment.destination_id == "zuhause"
    assert shipment.recipient_emails == ["max.mustermann@example.org"]


def test_pipeline_uebernimmt_geaenderte_zielorte(email_fixture: Loader):
    """Neue Zielorte wirken ab der naechsten Mail."""
    pipeline = MailPipeline(CarrierRegistry([EXAMPLE_PLUGIN]))
    (vorher,) = pipeline.process(email_fixture("utf8_plain.eml"))

    pipeline.set_destinations([ZUHAUSE])
    (nachher,) = pipeline.process(email_fixture("utf8_plain.eml"))

    assert vorher.destination_id == DESTINATION_UNKNOWN
    assert nachher.destination_id == "zuhause"


def test_nachbar_verfaelscht_die_zuordnung_nicht(email_fixture: Loader):
    """Die Anschrift des Nachbarn ist kein Zielort, auch wenn sie konfiguriert ist."""
    nebenan = Destination(
        id="nebenan", name="Nebenan", streets=["Musterstraße 3"], postal_codes=["12345"]
    )
    pipeline = MailPipeline(CarrierRegistry([DHL_PLUGIN]), destinations=[nebenan, ZUHAUSE])

    (shipment,) = pipeline.process(email_fixture("dhl/nachbar.eml"))

    assert shipment.destination_raw is None
    assert shipment.destination_id == "zuhause"


def test_abholort_der_packstation_ist_keine_zieladresse(email_fixture: Loader):
    """Der Abholort ist ein eigenes Feld und fliesst nicht in die Zuordnung."""
    pipeline = MailPipeline(CarrierRegistry([DHL_PLUGIN]), destinations=[PACKSTATION])

    (shipment,) = pipeline.process(email_fixture("dhl/packstation.eml"))

    assert shipment.pickup_location == "Packstation 123, Musterstraße 5, 12345 Musterstadt"
    assert shipment.destination_raw is None
    assert shipment.destination_id == DESTINATION_UNKNOWN


def test_merge_vereinigt_empfaengeradressen():
    """Jede Mail nennt ihre eigenen Empfaenger — zusammen ergeben sie das Bild."""
    erste = _shipment(recipient_emails=["max.mustermann@example.org"])
    zweite = _shipment(recipient_emails=["paket@example.org", "max.mustermann@example.org"])

    merged = merge_shipments(erste, zweite)

    assert merged.recipient_emails == ["max.mustermann@example.org", "paket@example.org"]
