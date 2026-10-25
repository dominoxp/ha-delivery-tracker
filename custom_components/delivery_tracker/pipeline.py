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

Zusaetzlich fuehrt jede Pipeline ein kurzes Protokoll der letzten Mails
(`MailLogEntry`) mit Betreff, Absender und Ergebnis. Es ist fuer den Nutzer
gedacht — die Sendungsverwaltung im Options-Flow zeigt damit, welche Mails
nicht erkannt wurden — und taucht deshalb nicht in den Diagnostics auf.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Self

from homeassistant.util import dt as dt_util

from .addresses import AddressMatcher, enrich_from_mail
from .carriers import CarrierRegistry
from .const import SOURCE_IMAP
from .mail import MailMessage, parse_mail
from .models import Destination, Shipment
from .sources import RawMail

_LOGGER = logging.getLogger(__name__)

# So viele Mails behaelt das Protokoll je Postfach.
MAIL_LOG_SIZE: Final = 100


class MailOutcome(StrEnum):
    """Ergebnis einer Mail — entspricht den vier Toepfen von `PipelineStats`."""

    MATCHED = "matched"
    WITHOUT_SHIPMENT = "without_shipment"
    UNMATCHED = "unmatched"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MailLogEntry:
    """Eine verarbeitete Mail im Protokoll."""

    processed_at: datetime
    outcome: MailOutcome
    received_at: datetime | None = None
    subject: str | None = None
    sender: str | None = None
    carrier: str | None = None
    shipments: tuple[tuple[str, str], ...] = ()
    """(ID, erkannter Status) je Sendung, die aus der Mail entstanden ist."""

    @classmethod
    def for_mail(
        cls,
        mail: MailMessage,
        outcome: MailOutcome,
        *,
        carrier: str | None = None,
        shipments: Iterable[Shipment] = (),
    ) -> Self:
        """Eintrag zu einer aufbereiteten Mail."""
        return cls(
            processed_at=dt_util.utcnow(),
            outcome=outcome,
            received_at=mail.date,
            subject=mail.subject or None,
            sender=mail.sender,
            carrier=carrier,
            shipments=tuple((shipment.id, shipment.status.value) for shipment in shipments),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisiert fuer den Store."""
        return {
            "processed_at": self.processed_at.isoformat(),
            "outcome": self.outcome.value,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "subject": self.subject,
            "sender": self.sender,
            "carrier": self.carrier,
            "shipments": [list(item) for item in self.shipments],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Aus dem Store lesen; wirft bei unlesbaren Eintraegen `ValueError`/`KeyError`."""
        processed_at = dt_util.parse_datetime(str(data["processed_at"]))
        if processed_at is None:
            raise ValueError("processed_at fehlt")
        received = data.get("received_at")
        return cls(
            processed_at=processed_at,
            outcome=MailOutcome(data["outcome"]),
            received_at=dt_util.parse_datetime(str(received)) if received else None,
            subject=data.get("subject"),
            sender=data.get("sender"),
            carrier=data.get("carrier"),
            shipments=tuple((str(item[0]), str(item[1])) for item in data.get("shipments") or []),
        )


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
        log: Iterable[MailLogEntry] = (),
    ) -> None:
        """Pipeline an eine Carrier-Registry und die Zielorte binden.

        `log` ist das gespeicherte Protokoll eines frueheren Laufs.
        """
        self._registry = registry
        self._source_id = source_id
        self._stats = PipelineStats()
        self._addresses = AddressMatcher(destinations)
        self._log: deque[MailLogEntry] = deque(log, maxlen=MAIL_LOG_SIZE)

    @property
    def stats(self) -> PipelineStats:
        """Die laufenden Kennzahlen."""
        return self._stats

    @property
    def log(self) -> list[MailLogEntry]:
        """Die letzten verarbeiteten Mails, aelteste zuerst."""
        return list(self._log)

    def clear_log(self) -> None:
        """Protokoll leeren, etwa vor dem Neu-Einlesen eines Postfachs."""
        self._log.clear()

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
            self._log.append(
                MailLogEntry(processed_at=dt_util.utcnow(), outcome=MailOutcome.FAILED)
            )
            _LOGGER.exception("Mail (UID %s) liess sich nicht aufbereiten", uid)
            return []
        return self.process_mail(mail)

    def process_mail(self, mail: MailMessage) -> list[Shipment]:
        """Eine bereits aufbereitete Mail zuordnen und auswerten."""
        resolution = self._registry.resolve(mail)
        plugin, shipments = resolution.plugin, resolution.shipments
        if plugin is None:
            self._stats.unmatched += 1
            self._log.append(MailLogEntry.for_mail(mail, MailOutcome.UNMATCHED))
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
                self._log.append(
                    MailLogEntry.for_mail(mail, MailOutcome.FAILED, carrier=plugin.carrier_id)
                )
                return []
            self._stats.without_shipment += 1
            self._log.append(
                MailLogEntry.for_mail(mail, MailOutcome.WITHOUT_SHIPMENT, carrier=plugin.carrier_id)
            )
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
        self._log.append(
            MailLogEntry.for_mail(
                mail, MailOutcome.MATCHED, carrier=plugin.carrier_id, shipments=shipments
            )
        )
        self._stats.shipments += len(shipments)
        self._stats.by_carrier[plugin.carrier_id] = self._stats.by_carrier.get(
            plugin.carrier_id, 0
        ) + len(shipments)
        return shipments

    def __repr__(self) -> str:
        """Kurzform fuer Logs und Diagnostics."""
        return f"<MailPipeline {self._source_id} verarbeitet={self._stats.processed}>"
