"""AliExpress — Bestellung, Zubringer, Sammelpaket und Uebergabe an Hermes/DHL.

AliExpress liefert nicht selbst. Wer dort bestellt, bekommt zwei Sorten Mails,
die nicht zusammenpassen:

- **Bestellmails** („Bestellung 8998…: Bestellung versandt") tragen nur die
  Bestellnummer, nie eine Trackingnummer.
- **Paketmails** („Package H1003…: with local carrier", „Packstueck …: mit
  lokalem Kurier") tragen eine Trackingnummer; die Bestellungen darin stehen
  nur im Ziel der Verfolgen-Links (`tradeOrderId`).

Dazu wechselt die Nummer unterwegs. Jeder Haendler schickt seine Bestellung
als Zubringer (`LP…`, `CNG…`) ins Cainiao-Lager, dort werden mehrere
Bestellungen zu einem Sammelpaket (`AP…`) gebuendelt, und das bekommt zuletzt
die Nummer von Hermes (`H…`) oder DHL (`00340434…`):

    Bestellung 8998…513 ─┐
    Bestellung 8550…513 ─┼─ LP2776… ─┐
    Bestellung 8179…513 ─┘           ├─ AP8922… ── 00340434060877185651 (DHL)
                                     │

Keine Mail nennt zwei dieser Nummern. Dieses Plugin liefert deshalb je Paket
die Menge seiner Bestellungen (`Shipment.order_numbers`); zusammengefuehrt
wird im Store (`merge.absorb_target`). Nummern der letzten Meile uebergibt die
Registry an Hermes bzw. DHL (`marketplace`) — dann landen deren eigene Mails
in derselben Sendung.

Eine Sammelmail („Your 2 packages have delivery updates") nennt mehrere Pakete
mit je eigener Statuszeile; welche Nummer zu welchem Block gehoert, steht nur
im Link des Blocks. Sie wird deshalb aus dem HTML gelesen.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, urlsplit

from homeassistant.util import dt as dt_util

from ...const import SOURCE_IMAP
from ...mail import MailMessage, html_to_text
from ...merge import build_shipment_id, describe_items
from ...models import Shipment, ShipmentEvent, ShipmentStatus, SourceRecord
from ...tracking import normalize_tracking_number
from ..base import RulesCarrier
from ..helpers import MAX_VALUE_LENGTH, map_status

SENDER: Final = "AliExpress"

_ORDER_URL: Final = "https://www.aliexpress.com/p/order/detail.html?orderId={order_number}"

_ORDER_SUBJECT: Final = re.compile(
    r"^(?:bestellung|order)\s+(?P<order>\d{12,20})\s*:", re.IGNORECASE
)
_PACKAGE_SUBJECT: Final = re.compile(
    r"(?:package|packst[uü]ck|paket|f[uü]r)\s+(?P<number>[A-Z]{0,4}\d{8,24}[A-Z]{0,3})\b",
    re.IGNORECASE,
)
_CLOSED_SUBJECT: Final = re.compile(
    r"^(?:your order|ihre bestellung)\s+(?P<order>\d{12,20})\s+(?:is closed|wurde geschlossen)",
    re.IGNORECASE,
)

# Die Verfolgen-Links. Nur ihr Ziel verbindet ein Paket mit seinen Bestellungen.
# Der Linktext endet spaetestens am naechsten `<a`: Mit `.*?` liefe jeder
# unverschlossene Link bis ans Mailende, und eine praeparierte Mail voller
# offener Links legte die Event-Loop sekundenlang lahm.
_LINK: Final = re.compile(
    r"<a\b[^>]*?\shref\s*=\s*[\"'](?P<href>[^\"']*?/p/tracking/[^\"']*)[\"'][^>]*>"
    r"(?P<inner>(?:(?!</?a\b).)*)</a\s*>",
    re.IGNORECASE | re.DOTALL,
)
_NUMBER: Final = re.compile(r"^[A-Z]{0,4}\d{8,24}[A-Z]{0,3}$")

# Abschnitte mit den Artikeln: Paketmails nennen sie „Package details" bzw.
# „Paketinfos", Bestellmails setzen sie unter den Verfolgen-Button.
_ITEMS_START: Final = re.compile(
    r"^(?:package details|paketinfos|bestellung verfolgen|track order)$", re.IGNORECASE
)
_ITEMS_END: Final = re.compile(
    r"^(?:more items|weitere artikel|ship to|versand nach|bestellsumme|order total|view more"
    r"|mehr anzeigen)\b",
    re.IGNORECASE,
)
# Die Mengenzeile schliesst einen Artikelblock ab: „x4", „60cm x4", „€ 4,99 x4".
_QUANTITY: Final = re.compile(r"(?:^|\s)[x×]\s?\d{1,4}$")  # noqa: RUF001
_DESTINATION_START: Final = re.compile(r"^(?:ship to|versand nach)$", re.IGNORECASE)
_GREETING: Final = re.compile(r"^(?:hi|hallo|hello|dear)\b[^\n]*,", re.IGNORECASE)
_NOISE_LINE: Final = re.compile(r"^(?:[.\s‌]*|\(?https?://\S*\)?|<url>)$")


def _clean(value: str | None) -> str | None:
    """Text saeubern; leer wird zu `None`."""
    if value is None:
        return None
    text = " ".join(value.split())
    return text[:MAX_VALUE_LENGTH] or None


def _lines(text: str) -> list[str]:
    """Die inhaltstragenden Zeilen eines Textes, Links entfernt."""
    found: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\(?https?://\S+\)?", "", raw).strip()
        if line and not _NOISE_LINE.match(line):
            found.append(line)
    return found


def items_in(lines: list[str]) -> list[str]:
    """Die Artikeltitel einer Zeilenfolge.

    Ein Artikel ist, was bis zu einer Mengenzeile steht: zuerst der (meist
    gekuerzte) Titel, darunter die Variante („60cm", „M3 (100pcs), 8mm").
    Weitere Varianten desselben Artikels stehen ohne Titel und ohne Menge am
    Ende der Liste — sie werden verworfen, statt eine Variante als Artikel
    auszugeben.
    """
    titles: list[str] = []
    block: list[str] = []
    for line in lines:
        if _QUANTITY.search(line):
            if block and (title := _clean(block[0])) and title not in titles:
                titles.append(title)
            block = []
        else:
            block.append(line)
    return titles


def item_section(lines: list[str]) -> list[str]:
    """Die Zeilen zwischen Artikel-Ueberschrift und dem Ende der Liste."""
    for index, line in enumerate(lines):
        if _ITEMS_START.match(line):
            section: list[str] = []
            for follow in lines[index + 1 :]:
                if _ITEMS_END.match(follow):
                    break
                section.append(follow)
            return section
    return []


def destination(lines: list[str]) -> str | None:
    """Die Lieferadresse unter „Versand nach" bzw. „Ship to".

    Hoechstens zwei Zeilen: Darunter folgen Name und Telefonnummer, die fuer
    die Zuordnung eines Zielorts nichts beitragen.
    """
    for index, line in enumerate(lines):
        if _DESTINATION_START.match(line):
            parts: list[str] = []
            for follow in lines[index + 1 : index + 3]:
                if "(+" in follow or _ITEMS_END.match(follow) or _GREETING.match(follow):
                    break
                parts.append(follow)
            return _clean(", ".join(parts))
    return None


def headline(lines: list[str]) -> str | None:
    """Die Statuszeile ueber der Anrede („Collected by local carrier")."""
    for index, line in enumerate(lines):
        if _GREETING.match(line):
            return _clean(lines[index - 1]) if index else None
    return None


def _links(html: str) -> Iterator[tuple[dict[str, list[str]], str]]:
    """Alle Verfolgen-Links: Parameter und Linktext."""
    for match in _LINK.finditer(html):
        params = parse_qs(urlsplit(unescape(match.group("href"))).query)
        yield params, match.group("inner")


def order_numbers(html: str | None) -> list[str]:
    """Alle Bestellungen aus den Verfolgen-Links, in Reihenfolge der Mail."""
    found: list[str] = []
    for params, _ in _links(html or ""):
        for value in params.get("tradeOrderId", []):
            if value.isdigit() and value not in found:
                found.append(value)
    return found


def _tracking(value: str) -> str | None:
    """Eine Trackingnummer aus Betreff oder Link, normalisiert."""
    number = normalize_tracking_number(value)
    return number if _NUMBER.match(number) else None


class AliExpressCarrier(RulesCarrier):
    """Bestellung, Paket und Sammelmail; der Status kommt aus dem Betreff."""

    marketplace = True

    def parse_all(self, mail: MailMessage) -> list[Shipment]:
        """Je Paket eine Sendung — oder eine fuer die Bestellung, solange es kein Paket gibt."""
        subject = " ".join(mail.subject.split())
        observed_at = mail.date or dt_util.utcnow()

        if (closed := _CLOSED_SUBJECT.search(subject)) is not None:
            shipment = self._order_shipment(mail, closed.group("order"), observed_at)
            shipment.cancelled = True
            return [shipment]

        lines = _lines(mail.text)
        packages = self._package_blocks(mail, observed_at)
        if packages and _PACKAGE_SUBJECT.search(subject) is None:
            return packages

        status, _ = map_status(subject, self.rules.status_map)
        if status is ShipmentStatus.UNKNOWN:
            # Bewertung, Empfangsbestaetigung, „Neuer Lieferstatus": keine
            # Aussage ueber das Paket.
            return []

        items = items_in(item_section(lines))
        orders = order_numbers(mail.html)
        package = _PACKAGE_SUBJECT.search(subject)
        number = _tracking(package.group("number")) if package else None
        if number is None and len(packages) == 1:
            number = packages[0].tracking_number

        if number is not None:
            status_text = headline(lines) or subject
            return [
                self._shipment(
                    mail,
                    observed_at,
                    tracking_number=number,
                    status=status,
                    status_text=status_text,
                    orders=orders,
                    items=items,
                    destination_raw=destination(lines),
                )
            ]

        order = _ORDER_SUBJECT.search(subject)
        if order is None:
            return []
        shipment = self._order_shipment(mail, order.group("order"), observed_at)
        shipment.status = status
        shipment.status_text = _clean(subject[order.end() :])
        shipment.items = items
        shipment.description = describe_items(items)
        shipment.destination_raw = destination(lines)
        shipment.events = [
            ShipmentEvent(timestamp=observed_at, status=status, text=shipment.status_text)
        ]
        return [shipment]

    def _package_blocks(self, mail: MailMessage, observed_at: datetime) -> list[Shipment]:
        """Die Pakete einer Sammelmail, je Verfolgen-Block mit eigener Statuszeile."""
        found: dict[str, Shipment] = {}
        for params, inner in _links(mail.html or ""):
            numbers = [n for value in params.get("trackingNumber", []) if (n := _tracking(value))]
            if not numbers:
                continue
            number = numbers[0]
            block = _lines(html_to_text(inner))
            label = block[0] if block else None
            status, _ = map_status(label or "", self.rules.status_map)
            titles = [
                title
                for line in block[1:]
                if not _QUANTITY.search(line) and (title := _clean(line))
            ][:1]
            orders = [value for value in params.get("tradeOrderId", []) if value.isdigit()]
            shipment = self._shipment(
                mail,
                observed_at,
                tracking_number=number,
                status=status,
                status_text=_clean(label) if status is not ShipmentStatus.UNKNOWN else None,
                orders=orders,
                items=titles,
            )
            if (known := found.get(number)) is not None:
                known.order_numbers += [
                    o for o in shipment.order_numbers if o not in known.order_numbers
                ]
                known.items += [i for i in shipment.items if i not in known.items]
                continue
            found[number] = shipment
        for shipment in found.values():
            shipment.order_number = shipment.order_numbers[0] if shipment.order_numbers else None
            shipment.description = describe_items(shipment.items)
        return list(found.values())

    def _shipment(
        self,
        mail: MailMessage,
        observed_at: datetime,
        *,
        tracking_number: str,
        status: ShipmentStatus,
        status_text: str | None,
        orders: list[str],
        items: list[str],
        destination_raw: str | None = None,
    ) -> Shipment:
        """Eine Paket-Sendung bauen."""
        events = []
        if status is not ShipmentStatus.UNKNOWN:
            events.append(ShipmentEvent(timestamp=observed_at, status=status, text=status_text))
        return Shipment(
            id=build_shipment_id(self.carrier_id, tracking_number),
            carrier=self.carrier_id,
            tracking_number=tracking_number,
            status=status,
            status_text=status_text,
            delivered_at=observed_at if status is ShipmentStatus.DELIVERED else None,
            destination_raw=destination_raw,
            sender=SENDER,
            description=describe_items(items),
            items=list(items),
            order_number=orders[0] if orders else None,
            order_numbers=list(orders),
            tracking_url=self.tracking_url(tracking_number),
            events=events,
            sources=[self._source(mail)],
            first_seen=observed_at,
            last_updated=observed_at,
        )

    def _order_shipment(
        self, mail: MailMessage, order_number: str, observed_at: datetime
    ) -> Shipment:
        """Die Sendung einer Bestellung, die noch keinem Paket zugeordnet ist."""
        return Shipment(
            id=build_shipment_id(self.carrier_id, sender=SENDER, order_number=order_number),
            carrier=self.carrier_id,
            sender=SENDER,
            order_number=order_number,
            order_numbers=[order_number],
            tracking_url=_ORDER_URL.format(order_number=order_number),
            sources=[self._source(mail)],
            first_seen=observed_at,
            last_updated=observed_at,
        )

    @staticmethod
    def _source(mail: MailMessage) -> SourceRecord:
        """Herkunftsnachweis der Mail."""
        return SourceRecord(
            source_id=SOURCE_IMAP,
            fetched_at=dt_util.utcnow(),
            reference=mail.message_id or None,
            subject=mail.subject or None,
            sender=mail.sender,
        )


PLUGIN = AliExpressCarrier.from_directory(Path(__file__).parent)


__all__ = [
    "PLUGIN",
    "SENDER",
    "AliExpressCarrier",
    "destination",
    "headline",
    "item_section",
    "items_in",
    "order_numbers",
]
