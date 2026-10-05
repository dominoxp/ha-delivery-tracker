"""GLS — Regelsatz plus Auswahl zwischen Paketnummer und Track ID.

GLS fuehrt jedes Paket unter zwei Kennungen: der Paketnummer (11 oder 12
Ziffern, die auf dem Etikett steht) und der kurzen Track ID (8 Zeichen). Nennt
eine Mail beide, wuerde die reine Regelauswertung daraus zwei Sendungen machen.
Die Paketnummer gewinnt: Sie ist die Kennung, die auch Drittdienste wie 17track
verstehen. Steht nur die Track ID in der Mail, bleibt es bei ihr.
"""

from __future__ import annotations

from pathlib import Path

from ...mail import MailMessage
from ...models import Shipment
from ..base import RulesCarrier
from ..helpers import build_shipments_from_rules, extract_tracking_numbers


class GlsCarrier(RulesCarrier):
    """Eine Sendung je Paket, auch wenn die Mail beide Kennungen nennt."""

    def tracking_numbers(self, mail: MailMessage) -> list[str]:
        """Die Paketnummern der Mail; nur ohne sie die Track IDs."""
        numbers = extract_tracking_numbers(self.rules, mail)
        parcel_numbers = [number for number in numbers if number.isdigit()]
        return parcel_numbers or numbers

    def parse_all(self, mail: MailMessage) -> list[Shipment]:
        """Die Regelauswertung mit der bereinigten Nummernliste."""
        return build_shipments_from_rules(
            self.rules, mail, tracking_numbers=self.tracking_numbers(mail)
        )


PLUGIN = GlsCarrier.from_directory(Path(__file__).parent)
