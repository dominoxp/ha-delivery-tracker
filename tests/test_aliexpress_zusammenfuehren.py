"""AliExpress-Pakete ueber Bestellung, Zubringer, Sammelpaket und letzte Meile zusammenfuehren.

Der Fall stammt aus einem echten Postfach: Vier Bestellungen gingen als
Zubringer ins Lager, wurden zum Sammelpaket `AP…` gebuendelt und kamen mit
DHL. Keine Mail nennt zwei der Nummern; ohne Zusammenfuehrung stuenden
Bestellungen, Zubringer, Sammelpaket und DHL-Paket als eigene Zeilen in der
Liste — die meisten davon fuer immer auf „unterwegs".
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.delivery_tracker.carriers import CarrierRegistry, discover_plugins
from custom_components.delivery_tracker.mail import parse_mail
from custom_components.delivery_tracker.merge import absorb_target, merge_items, merge_shipments
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus
from custom_components.delivery_tracker.store import ShipmentStore

Loader = Callable[[str], bytes]

CARRIERS = Path(__file__).parents[1] / "custom_components" / "delivery_tracker" / "carriers"
REGISTRY = CarrierRegistry(
    discover_plugins(CARRIERS, package="custom_components.delivery_tracker.carriers")
)
FIXTURES = Path(__file__).parent / "fixtures" / "emails" / "aliexpress"
T0 = datetime(2026, 4, 11, 8, 0, tzinfo=dt_util.UTC)


def _ingest(store: ShipmentStore, raw: bytes) -> None:
    """Eine Mail so verarbeiten wie der Coordinator: routen, mergen, aufraeumen."""
    for shipment in REGISTRY.resolve(parse_mail(raw)).shipments:
        if not shipment.cancelled:
            store.async_upsert(store.async_route(shipment))
    store.async_consolidate()


def _package(
    number: str | None,
    orders: list[str],
    *,
    carrier: str = "aliexpress",
    hours: float = 0,
    status: ShipmentStatus = ShipmentStatus.IN_TRANSIT,
) -> Shipment:
    when = T0 + timedelta(hours=hours)
    return Shipment(
        id=f"{carrier}_{number.lower()}" if number else f"aliexpress_order_aliexpress_{orders[0]}",
        carrier=carrier,
        tracking_number=number,
        status=status,
        sender="AliExpress",
        order_number=orders[0],
        order_numbers=list(orders),
        first_seen=when,
        last_updated=when,
    )


# ----------------------------------------------------------------------
# Die echte Kette
# ----------------------------------------------------------------------


def test_kette_wird_zu_einem_paket_je_zustellung(hass: HomeAssistant) -> None:
    """Alle Fixtures in Mail-Reihenfolge: uebrig bleiben die Pakete der letzten Meile."""
    store = ShipmentStore(hass)
    raws = [path.read_bytes() for path in FIXTURES.glob("*.eml")]
    for raw in sorted(raws, key=lambda raw: parse_mail(raw).date):
        _ingest(store, raw)

    by_id = {shipment.id: shipment for shipment in store.shipments}
    assert set(by_id) == {
        "dhl_00340434060877185651",
        "dhl_00340434805571222802",
        "hermes_h1003689856591323458",
    }

    paket = by_id["dhl_00340434060877185651"]
    assert paket.status is ShipmentStatus.DELIVERED
    assert paket.carrier == "dhl"
    assert paket.tracking_number == "00340434060877185651"
    assert paket.sender == "AliExpress"
    assert len(paket.order_numbers) == 4
    # Gekuerzte Titel aus Bestell- und Paketmail sind derselbe Artikel.
    assert len(paket.items) == len({item.split()[1] for item in paket.items})

    # Spaetere Mails zu frueheren Nummern landen beim Paket.
    assert store.resolve("aliexpress_ap89226395272617") == paket.id
    assert store.resolve("aliexpress_lp27760838749217") == paket.id
    assert store.resolve("aliexpress_order_aliexpress_8998771253088513") == paket.id

    assert by_id["dhl_00340434805571222802"].status is ShipmentStatus.IN_TRANSIT
    assert by_id["hermes_h1003689856591323458"].status is ShipmentStatus.DELIVERED


def test_neu_einlesen_aendert_nichts(hass: HomeAssistant) -> None:
    """Das Postfach erneut einlesen — auch in anderer Reihenfolge — laesst den Stand stehen.

    Aufgefallen auf der Testinstanz: Die alte „Delivery issue"-Mail setzte das
    zugestellte Paket wieder auf gescheitert, und eine spaete Sammelmail mit
    der Zubringer-Nummer machte aus dem Hermes-Paket wieder eines von AliExpress.
    """
    store = ShipmentStore(hass)
    raws = sorted(
        (path.read_bytes() for path in FIXTURES.glob("*.eml")), key=lambda raw: parse_mail(raw).date
    )
    for raw in raws:
        _ingest(store, raw)
    before = {shipment.id: shipment for shipment in store.shipments}

    for raw in reversed(raws):
        _ingest(store, raw)

    after = {shipment.id: shipment for shipment in store.shipments}
    assert set(after) == set(before)
    for shipment_id, shipment in after.items():
        assert (shipment.carrier, shipment.tracking_number, shipment.status) == (
            before[shipment_id].carrier,
            before[shipment_id].tracking_number,
            before[shipment_id].status,
        ), shipment_id
    assert after["dhl_00340434060877185651"].status is ShipmentStatus.DELIVERED
    assert after["hermes_h1003689856591323458"].carrier == "hermes"


def test_spaete_mail_zur_frueheren_stufe_laesst_den_zusteller_stehen(
    hass: HomeAssistant, email_fixture: Loader
) -> None:
    """Eine Mail zum Sammelpaket kommt erst nach der DHL-Zustellung.

    AliExpress meldet sich zu frueheren Nummern auch dann noch („Your 2
    packages…"). Die Mail ist die frischere, das Paket bleibt trotzdem DHL.
    """
    store = ShipmentStore(hass)
    for name in ("package_ready_to_ship.eml", "package_delivered.eml"):
        _ingest(store, email_fixture(f"aliexpress/{name}"))
    (sammel,) = REGISTRY.resolve(
        parse_mail(email_fixture("aliexpress/package_ready_to_ship.eml"))
    ).shipments
    later = sammel.last_updated + timedelta(days=30)
    spaet = replace(
        sammel,
        status=ShipmentStatus.IN_TRANSIT,
        events=[
            replace(event, timestamp=later, status=ShipmentStatus.IN_TRANSIT)
            for event in sammel.events
        ],
        first_seen=later,
        last_updated=later,
    )

    store.async_upsert(store.async_route(spaet))

    (paket,) = store.shipments
    assert paket.carrier == "dhl"
    assert paket.tracking_number == "00340434060877185651"
    assert paket.status is ShipmentStatus.DELIVERED


def test_abholbereit_nach_zustellproblem(hass: HomeAssistant, email_fixture: Loader) -> None:
    """Auf „Delivery issue" folgt „ready for pickup" — das Paket ist nicht verloren."""
    store = ShipmentStore(hass)
    _ingest(store, email_fixture("aliexpress/package_delivery_issue.eml"))
    _ingest(store, email_fixture("aliexpress/package_ready_for_pickup.eml"))

    (paket,) = store.shipments
    assert paket.status is ShipmentStatus.READY_FOR_PICKUP


# ----------------------------------------------------------------------
# Regeln
# ----------------------------------------------------------------------


def test_sammelpaket_geht_im_spaeter_gemeldeten_dhl_paket_auf_und_umgekehrt() -> None:
    """Die Richtung gibt der Zusteller vor, nicht die Reihenfolge der Mails."""
    orders = ["1", "2"]
    dhl = _package("00340434060877185651", orders, carrier="dhl", hours=0)
    sammel = _package("AP89226395272617", orders, hours=5)

    assert absorb_target(sammel, [dhl, sammel]) is dhl
    assert absorb_target(dhl, [dhl, sammel]) is None


def test_zubringer_geht_im_sammelpaket_auf() -> None:
    """Der Zubringer traegt eine der Bestellungen des Sammelpakets."""
    zubringer = _package("LP27760838749217", ["2"])
    sammel = _package("AP89226395272617", ["1", "2", "3"], hours=2)

    assert absorb_target(zubringer, [zubringer, sammel]) is sammel


def test_zwei_zubringer_derselben_bestellung_bleiben_getrennt() -> None:
    """Gleiche Bestellung, gleiche Stufe: zwei Pakete, keines loest das andere ab."""
    erster = _package("CNG00000000000001", ["1"])
    zweiter = _package("CNG00000000000002", ["1"], hours=3)

    assert absorb_target(erster, [erster, zweiter]) is None
    assert absorb_target(zweiter, [erster, zweiter]) is None


def test_bestellung_geht_in_der_spaetesten_stufe_auf() -> None:
    """Passen Zubringer, Sammelpaket und DHL-Paket, gewinnt das DHL-Paket."""
    bestellung = _package(None, ["2"])
    zubringer = _package("LP27760838749217", ["2"], hours=1)
    sammel = _package("AP89226395272617", ["1", "2"], hours=2)
    dhl = _package("00340434060877185651", ["1", "2"], carrier="dhl", hours=3)

    assert absorb_target(bestellung, [bestellung, zubringer, sammel, dhl]) is dhl


def test_bestellung_in_zwei_paketen_geht_im_aeltesten_auf() -> None:
    """Sonst stuende sie als „versandt" in der Liste, bis sie verjaehrt."""
    bestellung = _package(None, ["1"])
    erstes = _package("00340434060877185651", ["1"], carrier="dhl", hours=1)
    zweites = _package("00340434805571222802", ["1"], carrier="dhl", hours=2)

    assert absorb_target(bestellung, [zweites, bestellung, erstes]) is erstes


def test_zwei_pakete_der_letzten_meile_bleiben_getrennt() -> None:
    """Geteilte Lieferung: DHL und Hermes mit derselben Bestellung sind zwei Pakete."""
    dhl = _package("00340434060877185651", ["1"], carrier="dhl", status=ShipmentStatus.DELIVERED)
    hermes = _package("H1003689856591323458", ["1", "2"], carrier="hermes", hours=24)

    assert absorb_target(dhl, [dhl, hermes]) is None
    assert absorb_target(hermes, [dhl, hermes]) is None


def test_andere_absender_werden_nie_zusammengefuehrt() -> None:
    """Gleiche Bestellnummer heisst bei verschiedenen Haendlern nichts."""
    bestellung = _package(None, ["1"])
    fremd = _package("00340434060877185651", ["1"], carrier="dhl", hours=1)
    fremd.sender = "Anderer Shop"

    assert absorb_target(bestellung, [bestellung, fremd]) is None


def test_gekuerzte_artikel_sind_derselbe_artikel() -> None:
    """AliExpress schneidet denselben Titel je Mail an anderer Stelle ab."""
    merged = merge_items(
        ["Muster Rillenkugellager 5 x 1...", "Beispiel Linearfuehrung..."],
        ["Muster Rillenkugellager 5 x 13 x 4 m...", "Testartikel Tuerdaempfer"],
    )

    assert merged == [
        "Muster Rillenkugellager 5 x 13 x 4 m...",
        "Beispiel Linearfuehrung...",
        "Testartikel Tuerdaempfer",
    ]
    # Ungekuerzte, verschiedene Namen mit gleichem Anfang bleiben getrennt.
    assert merge_items(["Kabel"], ["Kabelbinder"]) == ["Kabel", "Kabelbinder"]


def test_anzeigetext_zaehlt_alle_artikel_des_pakets() -> None:
    """Nennt jede Stufe einen anderen Artikel, zeigt das Paket trotzdem beide an.

    Der Fall aus dem echten Postfach: Zubringer und Sammelpaket nannten je
    einen Artikel, das Hermes-Paket zeigte danach nur den ersten.
    """
    zubringer = _package("CNG00000000000001", ["1"])
    zubringer.items = ["Testartikel Sensor..."]
    zubringer.description = "Testartikel Sensor..."
    paket = _package("CNG00000000000001", ["1"], hours=1)
    paket.items = ["Muster LED-Streifen..."]
    paket.description = "Muster LED-Streifen..."

    merged = merge_shipments(zubringer, paket)

    assert merged.items == ["Testartikel Sensor...", "Muster LED-Streifen..."]
    assert merged.description == "Testartikel Sensor... (+1 weiterer Artikel)"


def test_beschreibung_des_zustellers_bleibt_stehen() -> None:
    """Eine Beschreibung, die nicht aus den Artikeln stammt, wird nicht ueberschrieben."""
    erste = _package("CNG00000000000001", ["1"])
    erste.items = ["Testartikel Sensor"]
    erste.description = "Elektronikbauteile"
    zweite = _package("CNG00000000000001", ["1"], hours=1)
    zweite.items = ["Muster LED-Streifen"]

    assert merge_shipments(erste, zweite).description == "Elektronikbauteile"


def test_nach_zustellproblem_zaehlt_nur_ein_neuerer_fortschritt() -> None:
    """„Abholbereit" nach „Zustellproblem" gewinnt, ein spaetes „unterwegs" nicht."""
    failed = _package(
        "00340434060877185651", ["1"], carrier="dhl", status=ShipmentStatus.DELIVERY_FAILED
    )
    pickup = _package(
        "00340434060877185651",
        ["1"],
        carrier="dhl",
        hours=2,
        status=ShipmentStatus.READY_FOR_PICKUP,
    )
    stale = _package("00340434060877185651", ["1"], carrier="dhl", hours=3)
    older_pickup = _package(
        "00340434060877185651",
        ["1"],
        carrier="dhl",
        hours=-2,
        status=ShipmentStatus.READY_FOR_PICKUP,
    )

    assert merge_shipments(failed, pickup).status is ShipmentStatus.READY_FOR_PICKUP
    assert merge_shipments(failed, stale).status is ShipmentStatus.DELIVERY_FAILED
    assert merge_shipments(failed, older_pickup).status is ShipmentStatus.DELIVERY_FAILED
