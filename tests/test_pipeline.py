"""Tests der Erkennungs-Pipeline: von der Rohmail zu Sendungen.

Gearbeitet wird mit dem mitgelieferten Beispiel-Plugin und echten `.eml`-Dateien
— damit deckt der Test dieselbe Strecke ab, die spaeter im Betrieb laeuft.
"""

from collections.abc import Callable

import pytest

from custom_components.delivery_tracker.carriers import CarrierPlugin, CarrierRegistry
from custom_components.delivery_tracker.carriers.example import PLUGIN as EXAMPLE_PLUGIN
from custom_components.delivery_tracker.const import SOURCE_IMAP
from custom_components.delivery_tracker.mail import MailMessage
from custom_components.delivery_tracker.models import Shipment, ShipmentStatus
from custom_components.delivery_tracker.pipeline import MailPipeline
from custom_components.delivery_tracker.sources import RawMail

Loader = Callable[[str], bytes]


@pytest.fixture
def pipeline() -> MailPipeline:
    """Pipeline mit dem mitgelieferten Beispiel-Zusteller."""
    return MailPipeline(CarrierRegistry([EXAMPLE_PLUGIN]))


def test_einfache_mail_ergibt_eine_sendung(pipeline: MailPipeline, email_fixture: Loader):
    """Der Normalfall: eine Versandbenachrichtigung, eine Sendung."""
    shipments = pipeline.process(email_fixture("utf8_plain.eml"), uid=1)

    assert len(shipments) == 1
    shipment = shipments[0]
    assert shipment.id == "example_ex1234567890de"
    assert shipment.carrier == "example"
    assert shipment.tracking_number == "EX1234567890DE"
    assert shipment.tracking_url == "https://track.example.com/de/EX1234567890DE"
    assert shipment.sender == "Kaffeerösterei Nord"
    assert shipment.description == "Kaffeebohnen 1kg"
    assert shipment.destination_raw == "Musterstraße 1, 12345 Musterstadt"
    assert [record.source_id for record in shipment.sources] == [SOURCE_IMAP]

    assert pipeline.stats.processed == 1
    assert pipeline.stats.matched == 1
    assert pipeline.stats.shipments == 1
    assert pipeline.stats.by_carrier == {"example": 1}


def test_html_mail_findet_die_nummer(pipeline: MailPipeline, email_fixture: Loader):
    """Auch ohne Text-Teil muss die Nummer aus Tabelle und Link herauskommen."""
    shipments = pipeline.process(email_fixture("html_only.eml"))

    assert [shipment.tracking_number for shipment in shipments] == ["EX9988776655DE"]
    assert shipments[0].status is ShipmentStatus.OUT_FOR_DELIVERY


def test_sammelbestaetigung_ergibt_drei_sendungen(pipeline: MailPipeline, email_fixture: Loader):
    """Eine Mail kann mehrere Sendungen tragen — die Pipeline liefert eine Liste."""
    shipments = pipeline.process(email_fixture("sammelbestaetigung.eml"))

    assert [shipment.tracking_number for shipment in shipments] == [
        "EX1111111111DE",
        "EX2222222222DE",
        "EX3333333333DE",
    ]
    # Jede Sendung bekommt eine eigene, stabile ID.
    assert len({shipment.id for shipment in shipments}) == 3
    # Der gemeinsame Kontext gilt fuer alle drei.
    assert all(shipment.order_number == "BST-99001" for shipment in shipments)
    assert pipeline.stats.shipments == 3
    assert pipeline.stats.matched == 1


def test_newsletter_wird_verworfen_und_gezaehlt(pipeline: MailPipeline, email_fixture: Loader):
    """Werbung ist der Normalfall im Postfach — sie muss zaehlbar durchfallen."""
    assert pipeline.process(email_fixture("newsletter.eml")) == []

    assert pipeline.stats.processed == 1
    assert pipeline.stats.unmatched == 1
    assert pipeline.stats.matched == 0
    assert pipeline.stats.failed == 0


def test_bekannter_zusteller_ohne_sendung_wird_getrennt_gezaehlt(pipeline: MailPipeline):
    """Werbung des Zustellers selbst ist etwas anderes als eine fremde Mail."""
    mail = MailMessage(
        message_id="<werbung@example.com>",
        subject="Ihre Sendung war zufriedenstellend? Beispiel Express bewerten",
        text="Wir freuen uns ueber Ihre Rueckmeldung.",
        from_address="marketing@example.com",
    )

    assert pipeline.process_mail(mail) == []
    assert pipeline.stats.without_shipment == 1
    assert pipeline.stats.unmatched == 0


def test_kaputte_bytes_kosten_nur_diese_mail(pipeline: MailPipeline, email_fixture: Loader):
    """Eine unlesbare Mail darf den Durchlauf nicht abbrechen."""
    assert pipeline.process(b"\x00\x01\x02 kein MIME") == []
    shipments = pipeline.process(email_fixture("utf8_plain.eml"))

    assert len(shipments) == 1
    assert pipeline.stats.processed == 2


def test_fehler_im_plugin_wird_gezaehlt_und_isoliert(caplog: pytest.LogCaptureFixture):
    """Ein Plugin, das wirft, darf die Pipeline nicht mitreissen."""

    class Boom(CarrierPlugin):
        def __init__(self) -> None:
            super().__init__("boom", "Boom")

        def matches(self, mail: MailMessage) -> int:
            return 100

        def parse(self, mail: MailMessage) -> Shipment | None:
            raise RuntimeError("kaputt")

    pipeline = MailPipeline(CarrierRegistry([Boom()]))

    assert pipeline.process_mail(MailMessage(message_id="<x@example.com>")) == []
    assert pipeline.stats.failed == 1
    assert "boom" in caplog.text


def test_handle_erfuellt_die_mailhandler_signatur(pipeline: MailPipeline, email_fixture: Loader):
    """Die Pipeline ist der `MailHandler`, den die IMAP-Quelle aus 1.1 erwartet."""
    raw_mail = RawMail(uid=42, raw=email_fixture("utf8_plain.eml"), folder="INBOX")

    shipments = pipeline.handle(raw_mail)

    assert [shipment.tracking_number for shipment in shipments] == ["EX1234567890DE"]


def test_diagnostics_enthalten_alle_kennzahlen(pipeline: MailPipeline, email_fixture: Loader):
    """Step 2.6 liest diese Zahlen aus; jede Mail landet in genau einem Topf."""
    pipeline.process(email_fixture("utf8_plain.eml"))
    pipeline.process(email_fixture("sammelbestaetigung.eml"))
    pipeline.process(email_fixture("newsletter.eml"))

    stats = pipeline.diagnostics()["stats"]

    assert stats["processed"] == 3
    assert (
        stats["matched"] + stats["unmatched"] + stats["without_shipment"] + stats["failed"]
        == stats["processed"]
    )
    assert stats["shipments"] == 4
    assert stats["by_carrier"] == {"example": 4}
