"""Amazon — Bestellung, Versand und Amazon Locker.

Amazon ist der schwierigste Fall dieser Integration, und zwar aus einem Grund,
den man erst an echten Mails sieht: **Es gibt keine Trackingnummer.** Weder im
Text noch im Betreff, nur im Ziel des „Lieferung verfolgen"-Buttons steht eine
`shipmentId` — Amazons eigene Paket-Kennung. Sie ist stabil und verbindet die
Versand- mit der spaeteren Zustellmail, also tritt sie hier an die Stelle der
Trackingnummer.

Dazu kommt das Layout. Eine Bestellbestaetigung ueber vier Artikel enthaelt
vier Paketbloecke mit je eigenem Liefertermin, eigener Adresse und eigenem
Artikel — alle unter derselben Bestellnummer:

    Zustellung: Montag
    Amazon Locker - Lindenhof, …
    Bestellnr.
    303-1234567-1234567
    Bestellung ansehen oder ändern
    https://www.amazon.de/your-orders/order-details?orderID=…
    * UGREEN USB Switch 2PC 2 In 4 Out …
      Menge: 1
      29.99 EUR

Ein reiner Regelsatz kann daraus nur *einen* Wert je Feld ziehen und wuerde die
vier Pakete zu einer Sendung verschmelzen. Deshalb schneidet dieses Plugin den
Text an den Zustellzeilen in Bloecke und baut je Block eine Sendung. Findet es
kein bekanntes Layout, faellt es auf die reine Regelauswertung zurueck — eine
Sendung mit Bestellnummer ist immer noch besser als keine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from homeassistant.util import dt as dt_util

from ...dates import parse_eta
from ...mail import MailMessage
from ...merge import build_shipment_id, describe_items
from ...models import Shipment
from ..base import RulesCarrier
from ..helpers import MAX_VALUE_LENGTH

# Woerter, mit denen eine Terminangabe beginnen kann.
_DATE_WORD: Final = (
    r"\d|heute|morgen|[uü]bermorgen|gestern"
    r"|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonnabend|sonntag"
    r"|januar|februar|m[aä]rz|april|mai|juni|juli|august|september|oktober|november|dezember"
)

# Zeilen, die einen Paketblock eroeffnen. Entscheidend ist der Vorausblick auf
# eine Terminangabe: Er trennt „Zustellung: Montag" sowohl von der nackten
# Fortschrittszeile „Zustellung" als auch vom Linktext „Lieferung verfolgen" —
# beide stehen in jeder Amazon-Mail und wuerden sonst Bloecke eroeffnen.
_BLOCK_START: Final = re.compile(
    rf"^(?:(?:zustellung|lieferung|ankunft)[^\S\n]*:?[^\S\n]*(?=(?:{_DATE_WORD}))"
    rf"|abholung am[^\S\n]*(?=(?:{_DATE_WORD}))"
    r"|(?:heute|gestern) abgeholt\b)",
    re.IGNORECASE | re.MULTILINE,
)

# Eine Artikelzeile der Bestelluebersicht: „* <Titel>", darunter Menge und Preis.
_ARTICLE: Final = re.compile(r"^\*[^\S\n]+(?P<value>\S[^\n]*)$", re.MULTILINE)

# Amazons Paket-Kennungen aus den Links. `shipmentId` steht in Versand- und
# Zustellmails, `pkgId` in den Locker-Mails; `packageIndex` ist immer 0 und
# taugt deshalb nicht zur Unterscheidung.
# Nur der Parametername ist unempfindlich gegen Gross-/Kleinschreibung — die
# Kennungen selbst sind es nicht („Tn0znYV69" ist nicht „tn0znyv69").
_SHIPMENT_ID: Final = re.compile(r"[?&](?i:shipmentId)=(?P<value>[A-Za-z0-9]{4,24})")
_PACKAGE_ID: Final = re.compile(r"[?&](?i:pkgId)=(?P<value>[A-Za-z0-9][A-Za-z0-9-]{7,40})")
# Eine Bestellbestaetigung kann mehrere Bestellungen zusammenfassen; jeder
# Paketblock nennt deshalb seine eigene Bestellnummer.
_ORDER_NUMBER: Final = re.compile(r"\b(?P<value>\d{3}-\d{7}-\d{7})\b")
_ACCESS_CODE: Final = re.compile(r"[?&](?i:accessCode)=(?P<value>[A-Za-z0-9]{4,12})")

# Der Verfolgen-Link. Er taugt als `tracking_url`, weil er Bestellnummer und
# `shipmentId` schon enthaelt — eine Vorlage mit nur einer Nummer koennte ihn
# gar nicht bilden. Die Klick-Zaehlparameter fliegen raus.
_TRACKER_URL: Final = re.compile(
    r"https?://[^\s()\[\]]*?/progress-tracker/package\?[^\s()\[\]]+", re.IGNORECASE
)
_NOISE_PARAM: Final = re.compile(r"&(?:vt|ref_|_encoding|noPtRedirect)=[^&]*", re.IGNORECASE)

# Ueberschriften, ab denen im selben Dokument nur noch Werbung folgt.
_MARKETING: Final = re.compile(
    r"empfehlungen f[uü]r (?:sie|dich)"
    r"|k[oö]nnten (?:ihnen|dir|sie)[^\n]{0,20}(?:gefallen|interessieren)"
    r"|inspiriert von (?:ihrem|deinem) einkauf"
    r"|kunden kauften auch",
    re.IGNORECASE,
)

# Produktlink als zweiter Weg zum Artikelnamen — aeltere Layouts verlinken den
# Titel, statt ihn als Listenzeile zu setzen.
_PRODUCT_LINE: Final = re.compile(
    r"(?:^|\n)[^\S\n]*(?:\d{1,3}\s*[x×]\s*)?"  # noqa: RUF001
    r"(?P<value>[^\n(]{5,150}?)\s*"
    r"\(https?://[^\s)]*?/(?:dp|gp/product)/[0-9A-Z]{10}",
    re.IGNORECASE,
)

# Linktexte, die zwar auf eine Produktseite zeigen, aber keinen Titel tragen.
_NO_TITLE: Final = re.compile(
    r"^(?:hier|jetzt|mehr|artikel|produkt|bestellung|zur bestellung|ansehen"
    r"|erneut kaufen|bewerten|weitere)\b|^\W*$",
    re.IGNORECASE,
)


def _clean(value: str | None) -> str | None:
    """Extrahierten Text saeubern; leer wird zu `None`."""
    if value is None:
        return None
    text = " ".join(value.split())
    return text[:MAX_VALUE_LENGTH] or None


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    """Den Wert des ersten Treffers oder `None`."""
    match = pattern.search(text)
    return _clean(match.group("value")) if match is not None else None


def articles(text: str) -> list[str]:
    """Alle Artikelzeilen eines Textstuecks."""
    return [title for match in _ARTICLE.finditer(text) if (title := _clean(match.group("value")))]


def product_items(mail: MailMessage) -> list[str]:
    """Alle Artikel einer Mail, ohne die Produktlinks aus dem Werbeblock."""
    text = mail.searchable_text
    if (marketing := _MARKETING.search(text)) is not None:
        text = text[: marketing.start()]

    if found := articles(text):
        return found

    # Aeltere Layouts verlinken den Titel, statt ihn als Liste zu setzen.
    for match in _PRODUCT_LINE.finditer(text):
        title = _clean(match.group("value"))
        if title and not _NO_TITLE.match(title):
            return [title]
    return []


def product_description(mail: MailMessage) -> str | None:
    """Den Anzeigetext zu den Artikeln einer Mail.

    `None`, wenn sich weder eine Artikelzeile noch ein Produktlink findet — ein
    fehlendes Feld ist besser als ein falsches.
    """
    return describe_items(product_items(mail))


@dataclass(frozen=True, slots=True)
class Package:
    """Ein Paketblock einer Amazon-Mail."""

    token: str
    """Was dieses Paket von den anderen der Bestellung unterscheidet."""
    eta_text: str | None = None
    destination: str | None = None
    items: tuple[str, ...] = ()
    pickup_location: str | None = None
    pickup_code: str | None = None
    tracking_url: str | None = None
    order_number: str | None = None
    """Bestellnummer dieses Blocks, falls er eine eigene nennt."""

    @property
    def description(self) -> str | None:
        """Anzeigetext zu den Artikeln dieses Pakets."""
        return describe_items(list(self.items))


def _tracking_url(block: str) -> str | None:
    """Den Verfolgen-Link eines Blocks, ohne die Klick-Zaehlparameter."""
    match = _TRACKER_URL.search(block)
    if match is None:
        return None
    return _NOISE_PARAM.sub("", match.group(0))


def _pickup_location(lines: list[str]) -> str | None:
    """Die Abholort-Zeile eines Blocks, falls es eine ist."""
    for line in lines[:3]:
        if re.match(r"amazon (?:locker|hub|counter)\b", line, re.IGNORECASE):
            return _clean(line)
    return None


def packages(text: str) -> list[Package]:
    """Den Mailtext in Paketbloecke schneiden.

    Eine leere Liste heisst: kein bekanntes Layout. Der Aufrufer faellt dann auf
    die reine Regelauswertung zurueck.
    """
    starts = [match.start() for match in _BLOCK_START.finditer(text)]
    if not starts:
        return []

    bounds = [*starts, len(text)]
    found: list[Package] = []
    for index in range(len(starts)):
        block = text[bounds[index] : bounds[index + 1]]
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        # Die Adresse steht direkt unter der Zustellzeile — es sei denn, dort
        # steht ein Link; eine URL ist keine Anschrift.
        destination = None
        if len(lines) > 1 and not lines[1].lower().startswith(("http://", "https://")):
            destination = _clean(lines[1])
        found.append(
            Package(
                token=_first(_SHIPMENT_ID, block) or _first(_PACKAGE_ID, block) or str(index),
                eta_text=_clean(lines[0]),
                destination=destination,
                items=tuple(articles(block)),
                pickup_location=_pickup_location(lines),
                pickup_code=_first(_ACCESS_CODE, block),
                tracking_url=_tracking_url(block),
                order_number=_first(_ORDER_NUMBER, block),
            )
        )
    return found


class AmazonCarrier(RulesCarrier):
    """Regelsatz plus Aufteilung der Mail in einzelne Pakete."""

    def parse_all(self, mail: MailMessage) -> list[Shipment]:
        """Je Paketblock eine Sendung bauen; sonst die Regelauswertung."""
        shipments = super().parse_all(mail)
        if not shipments:
            return shipments

        template = shipments[0]
        # Traegt die Mail ausnahmsweise doch Trackingnummern, bleibt es dabei —
        # sie sind der bessere Anker als jede Paket-Kennung.
        blocks = [] if template.tracking_number else packages(mail.searchable_text)
        if not blocks:
            items = product_items(mail)
            for shipment in shipments:
                shipment.items = shipment.items or items
                shipment.description = shipment.description or describe_items(items)
            return shipments

        observed_at = mail.date or dt_util.utcnow()
        return [self._from_package(template, package, observed_at) for package in blocks]

    def _from_package(self, template: Shipment, package: Package, observed_at: object) -> Shipment:
        """Aus Vorlage und Paketblock eine Sendung bauen."""
        eta_start, eta_end = None, None
        if package.eta_text and not template.is_closed:
            eta_start, eta_end = parse_eta(package.eta_text, now=observed_at)  # type: ignore[arg-type]

        order_number = package.order_number or template.order_number
        return Shipment(
            id=build_shipment_id(
                self.carrier_id,
                sender=template.sender,
                order_number=order_number,
                package=package.token,
            ),
            carrier=self.carrier_id,
            status=template.status,
            status_text=template.status_text,
            eta_start=eta_start,
            eta_end=eta_end,
            delivered_at=template.delivered_at,
            destination_raw=package.destination or template.destination_raw,
            sender=template.sender,
            description=package.description or template.description,
            items=list(package.items) or list(template.items),
            order_number=order_number,
            pickup_location=package.pickup_location or template.pickup_location,
            pickup_code=package.pickup_code or template.pickup_code,
            tracking_url=package.tracking_url,
            events=list(template.events),
            sources=list(template.sources),
            first_seen=template.first_seen,
            last_updated=template.last_updated,
        )


PLUGIN = AmazonCarrier.from_directory(Path(__file__).parent)


__all__ = [
    "PLUGIN",
    "AmazonCarrier",
    "Package",
    "articles",
    "packages",
    "product_description",
    "product_items",
]
