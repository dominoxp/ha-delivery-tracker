"""DHL Paket und Deutsche Post — ein Plugin mit zwei Regelsaetzen.

Beide Zusteller schicken aus denselben Absender-Domains und sind fuer den
Nutzer derselbe Zusteller; sie teilen sich deshalb die Carrier-ID `dhl`. Ihre
Nummernformate, Pruefverfahren und Formulierungen sind dagegen verschieden —
ein gemeinsamer Regelsatz muesste die Muster beider Seiten so weit oeffnen,
dass er Fehltreffer produziert.

Also liegen hier zwei Regeldateien: `rules.yaml` fuer DHL Paket und
`deutsche_post.yaml` fuer die Briefsendungen. Fuer jede Mail gewinnt der besser
bewertete Regelsatz; die Sendungs-ID bleibt in beiden Faellen `dhl_<nummer>`.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Final, Self

from ...mail import MailMessage
from ...models import Shipment
from ...tracking import ChecksumResult
from ..base import RulesCarrier
from ..helpers import build_shipments_from_rules, check_tracking_number, score_mail
from ..rules import RULES_FILENAME, CarrierRules, load_rules

# Der zweite Regelsatz neben der `rules.yaml`.
LETTER_RULES_FILENAME: Final = "deutsche_post.yaml"

# Einschreiben-Nummern im UPU-Format (`RR123456789DE`) gehoeren zur Brief-
# Sendungsverfolgung; alles andere wird ueber die Paketverfolgung nachgesehen.
_LETTER_NUMBER: Final = re.compile(r"^[A-Z]{2}\d{9}DE$", re.IGNORECASE)

# DHL laesst den Text-Teil seiner Mails markdown-artig auszeichnen:
# „Ihre **Express Online Seller **Sendung wird Ihnen **heute** … zugestellt".
# Die Sternchen stehen mitten in den Formulierungen, auf die die Regeln
# ansetzen, und muessen deshalb vor der Auswertung weg.
_EMPHASIS: Final = re.compile(r"\*{2,3}")


def plain(mail: MailMessage) -> MailMessage:
    """Die Auszeichnungs-Sternchen aus Betreff und Text nehmen."""
    if "**" not in mail.subject and "**" not in mail.text:
        return mail
    return replace(
        mail,
        subject=_EMPHASIS.sub("", mail.subject),
        text=_EMPHASIS.sub("", mail.text),
    )


class DhlCarrier(RulesCarrier):
    """Zusteller mit zwei Regelsaetzen, zwischen denen je Mail entschieden wird."""

    def __init__(self, parcel_rules: CarrierRules, letter_rules: CarrierRules) -> None:
        """Plugin aus Paket- und Brief-Regelsatz bauen."""
        super().__init__(parcel_rules)
        self.letter_rules = letter_rules

    @classmethod
    def from_directory(cls, directory: str | Path) -> Self:
        """Beide Regeldateien neben dem Plugin-Modul laden (blockierend)."""
        base = Path(directory)
        return cls(
            load_rules(base / RULES_FILENAME),
            load_rules(base / LETTER_RULES_FILENAME),
        )

    def rules_for(self, mail: MailMessage) -> CarrierRules:
        """Den Regelsatz waehlen, der besser zur Mail passt.

        Bei Gleichstand gewinnt der Paket-Regelsatz: Pakete sind der
        Normalfall, Briefsendungen mit Verfolgung die Ausnahme.
        """
        if score_mail(self.letter_rules, mail) > score_mail(self.rules, mail):
            return self.letter_rules
        return self.rules

    def matches(self, mail: MailMessage) -> int:
        """Die bessere der beiden Bewertungen."""
        clean = plain(mail)
        return max(score_mail(self.rules, clean), score_mail(self.letter_rules, clean))

    def parse_all(self, mail: MailMessage) -> list[Shipment]:
        """Die Mail mit dem passenden Regelsatz auswerten."""
        clean = plain(mail)
        return build_shipments_from_rules(self.rules_for(clean), clean)

    def tracking_url(self, tracking_number: str) -> str | None:
        """Die Verfolgungs-URL zum Format der Nummer waehlen."""
        if _LETTER_NUMBER.match(tracking_number.strip()):
            return self.letter_rules.tracking_url(tracking_number)
        return self.rules.tracking_url(tracking_number)

    def check_tracking_number(self, number: str) -> ChecksumResult | None:
        """Beide Regelsaetze befragen; eine stimmende Pruefziffer gewinnt."""
        results = [
            check_tracking_number(rules, number) for rules in (self.rules, self.letter_rules)
        ]
        if ChecksumResult.VALID in results:
            return ChecksumResult.VALID
        return next((result for result in results if result is not None), None)


PLUGIN = DhlCarrier.from_directory(Path(__file__).parent)
