"""Die Erkennungs-Pipeline: von der Rohmail zu Sendungen.

Sie klammert die drei Schritte zusammen, die zwischen Postfach und Datenmodell
liegen, und ist damit der `MailHandler`, den die IMAP-Quelle aus Step 1.1 erwartet:

```
Rohbytes ──▶ parse_mail ──▶ Registry.resolve ──▶ Plugin.parse_all ──▶ Zieladresse ──▶ [Shipment]
```

`resolve` fragt die zustaendigen Plugins in der Reihenfolge ihrer Bewertung,
bis eines eine Sendung liefert — der generische Fallback kommt also auch dann
noch zum Zug, wenn sich vorher ein spezifisches Plugin vergeblich gemeldet hat.

Die Zuordnung zur Zieladresse hier ist nur der erste Stand. Massgeblich ist die
Neubewertung nach dem Zusammenfuehren im Store (`ShipmentStore.async_assign_destinations`),
weil erst dort alle Mails einer Sendung beisammen sind.

Nichts davon darf werfen. Eine Mail, die niemand versteht, ist Normalbetrieb —
Postfaecher enthalten weit mehr Werbung als Paketmeldungen. Verworfene Mails
werden deshalb nicht geloggt und vergessen, sondern gezaehlt: Wenn ein Zusteller
sein Mailformat aendert, ist der Anstieg von `unmatched` das einzige Signal, das
darauf hinweist. Die Zaehler landen in den Diagnostics aus Step 2.6.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .addresses import AddressMatcher, enrich_from_mail
from .carriers import CarrierRegistry
from .const import SOURCE_IMAP
from .mail import MailMessage, parse_mail
from .models import Destination, Shipment
from .sources import RawMail

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineStats:
    """Kennzahlen eines Postfach-Durchlaufs.

    Die Summe aus `unmatched`, `without_shipment`, `failed` und `matched` ergibt
    `processed`; jede Mail landet in genau einem der vier Toepfe.
    """

    processed: int = 0
    matched: int = 0
    """Mails, aus denen mindestens eine Sendung entstanden ist."""
    unmatched: int = 0
    """Mails, fuer die sich kein Zusteller zustaendig fuehlte."""
    without_shipment: int = 0
    """Mails eines bekannten Zustellers ohne verwertbare Sendung (meist Werbung)."""
    failed: int = 0
    """Mails, an denen die Verarbeitung gescheitert ist — das sollte 0 bleiben."""
    shipments: int = 0
    by_carrier: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Kennzahlen fuer die Diagnostics."""
        return {
            "processed": self.processed,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "without_shipment": self.without_shipment,
            "failed": self.failed,
            "shipments": self.shipments,
            "by_carrier": dict(sorted(self.by_carrier.items())),
        }


class MailPipeline:
    """Verarbeitet Rohmails zu Sendungen und fuehrt dabei Buch."""

    def __init__(
        self,
        registry: CarrierRegistry,
        *,
        source_id: str = SOURCE_IMAP,
        destinations: Iterable[Destination] = (),
    ) -> None:
        """Pipeline an eine Carrier-Registry und die Zielorte binden."""
        self._registry = registry
        self._source_id = source_id
        self._stats = PipelineStats()
        self._addresses = AddressMatcher(destinations)

    @property
    def stats(self) -> PipelineStats:
        """Die laufenden Kennzahlen."""
        return self._stats

    @property
    def addresses(self) -> AddressMatcher:
        """Die aktuelle Zuordnung zu Zielorten."""
        return self._addresses

    def set_destinations(self, destinations: Iterable[Destination]) -> None:
        """Geaenderte Zielorte uebernehmen — wirkt ab der naechsten Mail."""
        self._addresses = AddressMatcher(destinations)

    def diagnostics(self) -> dict[str, Any]:
        """Zustand fuer Step 2.6."""
        return {"source_id": self._source_id, "stats": self._stats.to_dict()}

    def handle(self, raw_mail: RawMail) -> list[Shipment]:
        """`MailHandler`-Signatur: eine Mail der IMAP-Quelle verarbeiten."""
        return self.process(raw_mail.raw, uid=raw_mail.uid, folder=raw_mail.folder)

    def process(
        self, raw: bytes, *, uid: int | None = None, folder: str | None = None
    ) -> list[Shipment]:
        """Eine Rohmail zu Sendungen verarbeiten; Fehler kosten nur diese Mail."""
        self._stats.processed += 1
        try:
            mail = parse_mail(raw, uid=uid, folder=folder)
        except Exception:
            self._stats.failed += 1
            _LOGGER.exception("Mail (UID %s) liess sich nicht aufbereiten", uid)
            return []
        return self.process_mail(mail)

    def process_mail(self, mail: MailMessage) -> list[Shipment]:
        """Eine bereits aufbereitete Mail zuordnen und auswerten."""
        resolution = self._registry.resolve(mail)
        plugin, shipments = resolution.plugin, resolution.shipments
        if plugin is None:
            self._stats.unmatched += 1
            _LOGGER.debug(
                "Mail %s von %s verworfen: kein Zusteller zustaendig",
                mail.message_id,
                mail.from_address or "unbekannt",
            )
            return []

        if not shipments:
            if resolution.failed:
                # Geloggt hat bereits die Registry, mit Traceback.
                self._stats.failed += 1
                return []
            self._stats.without_shipment += 1
            _LOGGER.debug(
                "Mail %s gehoert zu '%s', enthaelt aber keine Sendung",
                mail.message_id,
                plugin.carrier_id,
            )
            return []

        try:
            enrich_from_mail(shipments, mail)
            self._addresses.assign_all(shipments)
        except Exception:
            # Eine fehlende Zuordnung ist kein Grund, die Sendung zu verlieren;
            # der Store bewertet beim naechsten Durchlauf ohnehin neu.
            _LOGGER.exception("Zieladresse fuer Mail %s nicht bestimmbar", mail.message_id)

        self._stats.matched += 1
        self._stats.shipments += len(shipments)
        self._stats.by_carrier[plugin.carrier_id] = self._stats.by_carrier.get(
            plugin.carrier_id, 0
        ) + len(shipments)
        return shipments

    def __repr__(self) -> str:
        """Kurzform fuer Logs und Diagnostics."""
        return f"<MailPipeline {self._source_id} verarbeitet={self._stats.processed}>"
