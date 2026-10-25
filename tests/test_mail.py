"""Tests fuer die Vorverarbeitung roher MIME-Nachrichten."""

from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from custom_components.delivery_tracker.mail import (
    MailMessage,
    decode_bytes,
    decode_header_value,
    html_to_text,
    parse_mail,
)

Loader = Callable[[str], bytes]

ALLE_FIXTURES = [
    "utf8_plain.eml",
    "iso8859_1.eml",
    "broken_charset.eml",
    "html_only.eml",
    "sammelbestaetigung.eml",
    "newsletter.eml",
]


# ----------------------------------------------------------------------
# Zeichensaetze
# ----------------------------------------------------------------------


def test_utf8_mail_wird_vollstaendig_dekodiert(email_fixture: Loader):
    """Kopfzeilen und Body einer UTF-8-Mail kommen unversehrt an."""
    mail = parse_mail(email_fixture("utf8_plain.eml"))

    # Der Halbgeviertstrich steht so in der Mail — er muss ihn unveraendert ueberleben.
    assert mail.subject == "Ihre Sendung ist unterwegs – Kaffeerösterei"  # noqa: RUF001
    assert "in Kürze zugestellt" in mail.text
    assert "Musterstraße 1, 12345 Musterstadt" in mail.text
    assert mail.from_address == "versand@example.com"
    assert mail.from_name == "Example Express"
    assert mail.from_domain == "example.com"
    assert mail.to_addresses == ("max.mustermann@example.org",)
    assert mail.date == datetime(2026, 7, 28, 7, 15, tzinfo=UTC)
    assert mail.message_id == "<utf8-plain@example.com>"
    assert mail.html is None


def test_iso8859_1_mail_wird_dekodiert(email_fixture: Loader):
    """ISO-8859-1 ist der zweithaeufigste Fall und muss genauso sauber ankommen."""
    mail = parse_mail(email_fixture("iso8859_1.eml"))

    assert mail.subject == "Ihre Sendung für Erika Musterfrau"
    assert "Größe: 30x20x10 cm" in mail.text
    assert "Bücherhaus Süd" in mail.text
    assert "Bahnhofstraße 5" in mail.text


def test_defekte_zeichensatzangabe_wird_repariert(email_fixture: Loader):
    """Eine erfundene Angabe (`x-unknown`) darf nicht ins Leere fuehren."""
    mail = parse_mail(email_fixture("broken_charset.eml"))

    assert mail.subject == "Ihre Sendung wurde übergeben"
    assert "Example Express übergeben" in mail.text
    assert "Größte zulässige Zustellzeit" in mail.text


@pytest.mark.parametrize("name", ALLE_FIXTURES)
def test_keine_fixture_wirft(email_fixture: Loader, name: str):
    """Grundversprechen des Moduls: keine Mail loest je eine Exception aus."""
    mail = parse_mail(email_fixture(name), uid=17, folder="INBOX")

    assert mail.message_id
    assert mail.text.strip()
    assert mail.uid == 17
    assert mail.folder == "INBOX"


def test_decode_bytes_faellt_auf_ersatzzeichen_zurueck():
    """Bytes, die kein Zeichensatz dekodiert, ergeben Text statt einer Exception."""
    # Alleinstehendes 0x81 ist weder gueltiges UTF-8 noch in cp1252 belegt.
    assert decode_bytes(b"a\x81b", "utf-8") == "a\x81b"
    assert decode_bytes(b"Gr\xfc\xdfe", "voellig-erfunden") == "Grüße"


def test_decode_header_value_verkraftet_muell():
    """Eine kaputte Kopfzeile liefert bestenfalls Rohtext, nie einen Fehler."""
    assert decode_header_value(None) == ""
    assert decode_header_value("=?UTF-8?Q?Gr=C3=BC=C3=9Fe?=") == "Grüße"
    assert decode_header_value("=?ERFUNDEN?Q?Gr=FC=DFe?=") == "Grüße"
    assert decode_header_value("Betreff\r\n  mit    Faltung") == "Betreff mit Faltung"


# ----------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------


def test_html_mail_liefert_lesbaren_text_mit_links(email_fixture: Loader):
    """Eine reine HTML-Mail wird lesbar — und behaelt die Links."""
    mail = parse_mail(email_fixture("html_only.eml"))

    assert mail.html is not None
    assert "Sendungsnummer: EX9988776655DE" in mail.text
    assert "Sendung verfolgen (https://track.example.com/de/EX9988776655DE)" in mail.text
    assert "Beste Grüße" in mail.text


def test_text_teil_mit_html_wird_bereinigt():
    """Manche Zusteller (DHL) schicken HTML im Text-Teil — Tags und Entities fallen weg."""
    raw = (
        b"Subject: Test\n"
        b"Content-Type: text/plain; charset=UTF-8\n\n"
        b"Abholbereit ab dem n&auml;chsten&nbsp;Werktag.<br />\n"
        b'<a href="https://example.org/shop">Paketshop 785</a><br />\n'
    )

    mail = parse_mail(raw)

    assert "<" not in mail.text
    assert "ab dem nächsten Werktag." in mail.text
    assert "Paketshop 785 (https://example.org/shop)" in mail.text


def test_reiner_text_bleibt_unveraendert():
    """Ohne Tags wird nichts umgewandelt — auch kein „&" in Firmennamen."""
    raw = (
        b"Subject: Test\nContent-Type: text/plain; charset=UTF-8\n\n"
        b"Absender: Mueller & Soehne\nSendungsnummer: 123\n"
    )

    assert parse_mail(raw).text == "Absender: Mueller & Soehne\nSendungsnummer: 123"


def test_html_mail_verwirft_zaehlpixel_und_skripte(email_fixture: Loader):
    """Zaehlpixel, Stylesheets und Skripte sind kein Mailinhalt."""
    mail = parse_mail(email_fixture("html_only.eml"))

    assert "o.gif" not in mail.text
    assert "color: #333333" not in mail.text
    # Die Nummer aus dem Skript darf nie im Text landen, sonst wird sie erkannt.
    assert "EX0000000000DE" not in mail.text


def test_html_to_text_haelt_tabellenzellen_auseinander():
    """Ohne Trenner klebten Beschriftung und Wert zu einem Wort zusammen."""
    text = html_to_text("<table><tr><td>Sendungsnummer:</td><td>EX1DE</td></tr></table>")

    assert text == "Sendungsnummer: EX1DE"


def test_html_to_text_laesst_redundante_urls_weg():
    """Steht die URL schon im Linktext, waere die Wiederholung nur Rauschen."""
    html = '<a href="https://track.example.com/x">https://track.example.com/x</a>'

    assert html_to_text(html) == "https://track.example.com/x"


def test_html_to_text_ignoriert_unbrauchbare_ziele():
    """`mailto:` und Anker tragen nie eine Trackingnummer."""
    html = '<a href="mailto:service@example.com">Kontakt</a><a href="#oben">Nach oben</a>'

    assert html_to_text(html) == "Kontakt Nach oben"


def test_html_to_text_verkraftet_kaputtes_markup():
    """Mail-HTML ist selten wohlgeformt; abbrechen ist keine Option."""
    text = html_to_text("<div><p>Hallo <b>Max<div>Sendungsnummer: EX1DE")

    assert "Hallo" in text
    assert "Sendungsnummer: EX1DE" in text


def test_leeres_html_bleibt_leer():
    """Kein Inhalt, kein Text — und trotzdem kein Fehler."""
    assert html_to_text("") == ""


# ----------------------------------------------------------------------
# MIME-Struktur
# ----------------------------------------------------------------------


def test_mehrteilige_mail_nimmt_html_wenn_der_textteil_nur_platzhalter_ist(
    email_fixture: Loader,
):
    """Viele Zusteller legen in den Text-Teil nur „benoetigt HTML"."""
    mail = parse_mail(email_fixture("sammelbestaetigung.eml"))

    assert mail.html is not None
    assert "benoetigt HTML" not in mail.text
    assert "EX1111111111DE" in mail.text
    assert "EX3333333333DE" in mail.text


def test_base64_wird_dekodiert():
    """`base64` ist neben `quoted-printable` die uebliche Kodierung."""
    message = EmailMessage()
    message["From"] = "versand@example.com"
    message["Subject"] = "Ihre Sendung"
    message.set_content("Sendungsnummer: EX1234567890DE\nGrüße", cte="base64")

    mail = parse_mail(message.as_bytes())

    assert "Sendungsnummer: EX1234567890DE" in mail.text
    assert "Grüße" in mail.text


def test_anhang_wird_nicht_zum_body():
    """Ein Textanhang (Rechnung) gehoert nicht in den durchsuchten Text."""
    message = EmailMessage()
    message["From"] = "versand@example.com"
    message.set_content("Sendungsnummer: EX1234567890DE")
    message.add_attachment(
        b"Rechnungsnummer: R-4711",
        maintype="text",
        subtype="plain",
        filename="rechnung.txt",
    )

    mail = parse_mail(message.as_bytes())

    assert "EX1234567890DE" in mail.text
    assert "R-4711" not in mail.text


def test_kaputte_bytes_ergeben_ein_leeres_objekt():
    """Selbst voelliger Unsinn muss ein `MailMessage` ergeben."""
    mail = parse_mail(b"das ist keine Mail")

    assert isinstance(mail, MailMessage)
    assert mail.from_address == ""
    assert mail.date is None


def test_ersatz_id_ist_stabil_und_unterscheidet():
    """Ohne `Message-ID` braucht es eine ID, die dieselbe Mail wiedererkennt."""
    erste = parse_mail(b"Subject: Test\n\nInhalt")
    gleiche = parse_mail(b"Subject: Test\n\nInhalt")
    andere = parse_mail(b"Subject: Test\n\nAnderer Inhalt")

    assert erste.message_id == gleiche.message_id
    assert erste.message_id != andere.message_id


def test_list_unsubscribe_markiert_massenversand(email_fixture: Loader):
    """Newsletter tragen `List-Unsubscribe` — das Signal braucht die Bewertung."""
    assert parse_mail(email_fixture("newsletter.eml")).is_bulk is True
    assert parse_mail(email_fixture("utf8_plain.eml")).is_bulk is False


def test_empfaenger_aus_allen_kopfzeilen():
    """Bei Weiterleitungen steht die echte Adresse nur in `Delivered-To`."""
    raw = (
        b"From: versand@example.com\n"
        b"To: Max <max@example.org>\n"
        b"Cc: partner@example.org\n"
        b"Delivered-To: WEITERLEITUNG@example.org\n"
        b"Subject: Ihre Sendung\n\n"
        b"Text"
    )

    mail = parse_mail(raw)

    assert mail.to_addresses == (
        "max@example.org",
        "partner@example.org",
        "weiterleitung@example.org",
    )
