"""Gemeinsame Schnittstelle aller Datenquellen.

Eine Quelle liefert Sendungs-Fragmente: Sie fuellt genau die Felder, die sie
kennt, und laesst alles andere `None`. Das Zusammenfuehren mehrerer Sichten auf
dieselbe Sendung uebernimmt `merge.merge_shipments`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from ..merge import source_priority
from ..models import Shipment

if TYPE_CHECKING:
    from ..carriers import CarrierRegistry


class SourceError(Exception):
    """Basisfehler aller Quellen. Alles andere gilt dem Coordinator als voruebergehend."""


class SourcePermanentError(SourceError):
    """Fehler, den nur eine Konfigurationsaenderung behebt — Wiederholen ist zwecklos."""


class SourceAuthError(SourcePermanentError):
    """Zugangsdaten oder API-Schluessel werden abgelehnt; fuehrt zum Reauth-Hinweis."""


class SourceQuotaError(SourcePermanentError):
    """Das Kontingent des API-Schluessels ist aufgebraucht.

    Dauerhaft, weil ein erneuter Versuch es nur weiter belastet; die Quelle ruht
    bis zu einem neuen Schluessel oder dem naechsten Reload.
    """


class SourceRateLimitError(SourceError):
    """Der Dienst drosselt die Anfragen — voruebergehend, die Quelle wartet ab."""


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Was eine API-Quelle ueber den Rest der Integration wissen darf."""

    known_shipments: Callable[[], list[Shipment]]
    """Der aktuelle Bestand des Stores — daraus erfaehrt die API von neuen Nummern."""
    registry: CarrierRegistry | None = None
    """Die aktiven Carrier-Plugins, fuer die Pruefung und Zuordnung von Nummern."""


class TrackingSource(ABC):
    """Basisklasse fuer E-Mail-, API- und kuenftige Quellen."""

    def __init__(self, source_id: str, *, priority: int | None = None) -> None:
        """Quelle mit ihrer ID und Wertigkeit anlegen.

        Ohne explizite Angabe kommt die Wertigkeit aus `const.SOURCE_PRIORITIES`;
        sie entscheidet beim Merge, welche Quelle einen Konflikt gewinnt.
        """
        self.source_id = source_id
        self.priority = source_priority(source_id) if priority is None else priority

    @property
    def warning(self) -> SourceError | None:
        """Ein Fehler, mit dem die Quelle eingeschraenkt weiterlaeuft.

        Anders als eine Exception aus `async_fetch` kommen die Ergebnisse
        trotzdem an — etwa wenn das Kontingent fuer neue Nummern aufgebraucht
        ist, bekannte aber weiter abgefragt werden. Der Coordinator macht
        daraus einen Repairs-Eintrag.
        """
        return None

    def diagnostics(self) -> dict[str, Any]:
        """Zustand fuer den Diagnostics-Download; ohne Zugangsdaten."""
        return {}

    async def async_setup(self) -> None:  # noqa: B027 — bewusst optionaler Haken
        """Verbindungen aufbauen und Konfiguration pruefen.

        Wird einmal vor dem ersten `async_fetch` aufgerufen. Quellen ohne
        Verbindungsaufbau (etwa dateibasierte Tests) brauchen nichts zu tun.
        """

    @abstractmethod
    async def async_fetch(self) -> list[Shipment]:
        """Den aktuellen Stand der Quelle abholen.

        Rueckgabe sind Sendungs-Fragmente; eine leere Liste ist ein gueltiges
        Ergebnis. Fehler duerfen als Exception hochkommen — der Coordinator
        isoliert sie, damit eine kaputte Quelle die anderen nicht mitreisst.
        """

    async def async_close(self) -> None:  # noqa: B027 — bewusst optionaler Haken
        """Ressourcen freigeben. Muss mehrfach aufrufbar sein."""

    def __repr__(self) -> str:
        """Kurzform fuer Logs und Diagnostics."""
        return f"<{type(self).__name__} {self.source_id} prio={self.priority}>"


ApiSourceFactory = Callable[[HomeAssistant, Mapping[str, Any], SourceContext], TrackingSource]
"""Baut eine API-Quelle aus den Daten ihres `api_source`-Subentrys."""

API_SOURCE_FACTORIES: dict[str, ApiSourceFactory] = {}
"""Fabriken je Quellentyp (`CONF_SOURCE_TYPE`); die Quellen tragen sich beim
Import selbst ein. Ein Subentry ohne Fabrik wird beim Setup als Fehler der
Quelle vermerkt."""
