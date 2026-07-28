"""Schnittstelle der Carrier-Plugins.

Ein Plugin beantwortet zwei Fragen: „Gehoert diese Mail zu mir?" (`matches`, als
Bewertung, damit bei mehreren Kandidaten der beste gewinnt) und „Was steht drin?"
(`parse`). In `parse` darf ein Plugin davon ausgehen, dass die Mail ihm gehoert.

Die meisten Zusteller kommen mit `RulesCarrier` aus — dann besteht das ganze
Plugin aus einer `rules.yaml` und einer Zeile Python.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Self

from ..mail import MailMessage
from ..models import Shipment
from .helpers import build_shipments_from_rules, score_mail
from .rules import RULES_FILENAME, CarrierRules, load_rules


class CarrierPlugin(ABC):
    """Basisklasse aller Zusteller-Plugins."""

    def __init__(self, carrier_id: str, name: str, *, priority: int = 0) -> None:
        """Plugin mit seiner ID, dem Anzeigenamen und seiner Rangfolge anlegen."""
        self.carrier_id = carrier_id
        self.name = name
        self.priority = priority
        """Entscheidet bei gleicher Bewertung, welches Plugin die Mail bekommt."""

    @abstractmethod
    def matches(self, mail: MailMessage) -> int:
        """Bewerten, wie gut die Mail zu diesem Zusteller passt.

        0 heisst „nicht meine Mail". Groessere Werte gewinnen gegen kleinere,
        die Skala orientiert sich an den Gewichten in `helpers`.
        """

    @abstractmethod
    def parse(self, mail: MailMessage) -> Shipment | None:
        """Die Mail in eine Sendung uebersetzen.

        `None` heisst: Die Mail gehoert zwar zu diesem Zusteller, enthaelt aber
        keine verwertbare Sendung (z.B. reine Werbung).
        """

    def parse_all(self, mail: MailMessage) -> list[Shipment]:
        """Alle Sendungen der Mail — eine Sammelbestaetigung enthaelt mehrere.

        Die Standardfassung reicht `parse` durch; Plugins, die mehrere Sendungen
        je Mail liefern koennen, ueberschreiben diese Methode.
        """
        shipment = self.parse(mail)
        return [shipment] if shipment is not None else []

    def tracking_url(self, tracking_number: str) -> str | None:
        """Die Sendungsverfolgungs-URL zu einer Nummer bilden."""
        return None

    def __repr__(self) -> str:
        """Kurzform fuer Logs und Diagnostics."""
        return f"<{type(self).__name__} {self.carrier_id}>"


class RulesCarrier(CarrierPlugin):
    """Ein vollstaendig ueber `rules.yaml` beschriebenes Plugin.

    Zusteller mit Eigenheiten leiten davon ab und ueberschreiben `parse` oder
    `matches`, koennen aber ueber `super()` auf die Regelauswertung zurueckgreifen.
    """

    def __init__(self, rules: CarrierRules) -> None:
        """Plugin aus bereits geladenen Regeln bauen."""
        super().__init__(rules.carrier_id, rules.name, priority=rules.priority)
        self.rules = rules

    @classmethod
    def from_directory(cls, directory: str | Path) -> Self:
        """Die `rules.yaml` neben dem Plugin-Modul laden (blockierend)."""
        return cls(load_rules(Path(directory) / RULES_FILENAME))

    def matches(self, mail: MailMessage) -> int:
        """Bewertung aus den Match-Regeln."""
        return score_mail(self.rules, mail)

    def parse(self, mail: MailMessage) -> Shipment | None:
        """Die erste Sendung aus den Extraktionsregeln."""
        shipments = self.parse_all(mail)
        return shipments[0] if shipments else None

    def parse_all(self, mail: MailMessage) -> list[Shipment]:
        """Alle Sendungen aus den Extraktionsregeln."""
        return build_shipments_from_rules(self.rules, mail)

    def tracking_url(self, tracking_number: str) -> str | None:
        """URL aus der Vorlage in den Regeln."""
        return self.rules.tracking_url(tracking_number)
