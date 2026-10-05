"""Generischer Fallback fuer Mails, die kein spezifisches Plugin versteht.

Typischer Fall: Ein Shop schreibt selbst „Ihre Bestellung wurde versandt" und
nennt die Trackingnummer eines Zustellers, fuer den es kein Plugin gibt. Diese
Nummer soll wenigstens erfasst werden, damit eine API sie spaeter anreichern
kann (Step 3.1).

Das ist der gefaehrlichste Teil der Integration. Ein Shop-Mail steckt voller
Zahlen — Bestell-, Rechnungs-, Kunden-, Telefonnummern —, und jede, die als
Sendung durchrutscht, bleibt wochenlang im Dashboard stehen. Deshalb gilt:

* Angenommen wird nur, was eine **stimmende Pruefziffer** hat. Ein Format ohne
  Pruefverfahren liefert hier nie etwas, egal wie gut der Kontext ist.
* Ein **ausschliessendes Kontextwort** („Rechnungsnummer") verwirft auch eine
  formal gueltige Nummer — anders als bei den spezifischen Plugins, die sich
  auf ihre Absender-Domain stuetzen koennen.
* Reine Ziffernfolgen brauchen zusaetzlich eine **Ankuendigung** oder einen
  Tracking-Parameter im Link: Bei 18 oder 20 Ziffern stimmt die GS1-Pruefziffer
  zufaellig in jedem zehnten Fall.

Im Zweifel liefert das Plugin nichts. Den Zusteller leitet es aus dem
Nummernformat oder der Absender-Domain ab; was sich nicht erschliesst, bleibt
`generic`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from ...mail import MailMessage
from ...merge import build_shipment_id
from ...models import Shipment
from ...tracking import MAX_TRACKING_NUMBERS, ChecksumResult, iter_candidates
from ..base import RulesCarrier
from ..helpers import build_shipments_from_rules, domain_matches

# UPS-Nummern sind am Praefix eindeutig.
_UPS: Final = re.compile(r"^1Z[0-9A-Z]{16}$")

# S10-Nummern tragen das Land des einliefernden Postunternehmens als Suffix.
_S10: Final = re.compile(r"^[A-Z]{2}\d{9}(?P<country>[A-Z]{2})$")
_S10_CARRIERS: Final[dict[str, str]] = {
    "DE": "dhl",
    "AT": "post_at",
    "CH": "post_ch",
    "NL": "postnl",
    "GB": "royal_mail",
    "US": "usps",
}

# Absender-Domains von Zustellern ohne eigenes Plugin. Zusteller mit Plugin
# stehen hier nicht: Ihre Mails landen gar nicht erst beim Fallback.
_DOMAIN_CARRIERS: Final[dict[str, str]] = {
    "fedex.com": "fedex",
    "tnt.com": "tnt",
    "postnl.nl": "postnl",
    "postnl.com": "postnl",
    "post.at": "post_at",
    "post.ch": "post_ch",
    "swisspost.ch": "post_ch",
    "usps.com": "usps",
    "royalmail.com": "royal_mail",
    "general-overnight.com": "go_express",
    "dbschenker.com": "db_schenker",
}


def carrier_for(number: str, from_domain: str = "") -> str | None:
    """Den Zusteller einer Nummer ableiten; `None`, wenn er sich nicht erschliesst.

    Das Format gewinnt vor der Domain: Ein Shop, der eine UPS-Nummer nennt,
    hat per UPS verschickt, egal von welcher Adresse er schreibt.
    """
    if _UPS.match(number):
        return "ups"
    if (match := _S10.match(number)) is not None and (
        carrier := _S10_CARRIERS.get(match.group("country"))
    ):
        return carrier
    return _carrier_by_domain(from_domain)


def _carrier_by_domain(from_domain: str) -> str | None:
    """Den Zusteller hinter einer Absender-Domain, falls es einer ist."""
    for domain, carrier in _DOMAIN_CARRIERS.items():
        if domain_matches(from_domain, [domain]):
            return carrier
    return None


class GenericCarrier(RulesCarrier):
    """Fallback mit absichtlich niedrigster Bewertung und strengster Auswahl."""

    def tracking_numbers(self, mail: MailMessage) -> list[str]:
        """Alle Nummern, die den strengen Massstab des Fallbacks bestehen."""
        found: dict[str, None] = {}
        for candidate in iter_candidates(
            mail.searchable_text, self.rules.tracking_patterns, schemes=self.rules.checksum
        ):
            if candidate.checksum is not ChecksumResult.VALID or candidate.context < 0:
                continue
            if candidate.number.isdigit() and candidate.context <= 0:
                continue
            found.setdefault(candidate.number)
            if len(found) >= MAX_TRACKING_NUMBERS:
                break
        return list(found)

    def matches(self, mail: MailMessage) -> int:
        """1 bei einer gueltigen Nummer, sonst 0 — nie mehr als das schwaechste Signal."""
        return 1 if self.tracking_numbers(mail) else 0

    def parse_all(self, mail: MailMessage) -> list[Shipment]:
        """Je gueltiger Nummer eine Sendung; ohne Nummer nichts, auch keine Bestellung.

        Eine Bestellnummer allein laesst sich ueber keine API verfolgen und
        wuerde jede Shop-Mail zur Sendung machen.
        """
        numbers = self.tracking_numbers(mail)
        if not numbers:
            return []

        # Schreibt kein bekannter Zusteller, ist der Absender der Haendler.
        merchant = mail.from_name if _carrier_by_domain(mail.from_domain) is None else None
        shipments = build_shipments_from_rules(self.rules, mail, tracking_numbers=numbers)
        for shipment in shipments:
            number = shipment.tracking_number or ""
            shipment.sender = shipment.sender or merchant
            shipment.carrier = carrier_for(number, mail.from_domain) or self.carrier_id
            shipment.id = build_shipment_id(shipment.carrier, number)
        return shipments


PLUGIN = GenericCarrier.from_directory(Path(__file__).parent)


__all__ = ["PLUGIN", "GenericCarrier", "carrier_for"]
