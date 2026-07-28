"""Datenmodell der Delivery-Tracker-Integration.

Alle Modelle sind reine Datencontainer ohne HA-Abhaengigkeit und serialisieren sich
selbst nach `dict` (fuer den Store und die Diagnostics). Zeitstempel sind
ausnahmslos timezone-aware und werden als UTC-ISO-String abgelegt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Self

from homeassistant.util import dt as dt_util

# Schema-Version der Serialisierung. Wird in jedes `to_dict` geschrieben, damit
# spaetere Feldaenderungen migriert statt geraten werden koennen.
MODEL_VERSION: Final = 1


class ShipmentStatus(StrEnum):
    """Normalisierter Sendungsstatus.

    Die Deklarationsreihenfolge ist zugleich die Fortschritts-Rangfolge: Ein Status
    mit hoeherem Rang loest einen niedrigeren ab, aber nie umgekehrt (siehe `merge`).
    """

    UNKNOWN = "unknown"
    ORDERED = "ordered"
    LABEL_CREATED = "label_created"
    IN_TRANSIT = "in_transit"
    DELAYED = "delayed"
    CUSTOMS = "customs"
    OUT_FOR_DELIVERY = "out_for_delivery"
    READY_FOR_PICKUP = "ready_for_pickup"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"
    RETURNED = "returned"

    @property
    def rank(self) -> int:
        """Position in der Fortschritts-Rangfolge."""
        return _STATUS_RANKS[self]

    @property
    def is_closed(self) -> bool:
        """True, wenn die Sendung abgeschlossen ist und der Retention verfaellt."""
        return self in CLOSED_STATUSES


_STATUS_RANKS: Final[dict[ShipmentStatus, int]] = {
    status: index for index, status in enumerate(ShipmentStatus)
}

# Status, die eine Sendung endgueltig abschliessen.
CLOSED_STATUSES: Final[frozenset[ShipmentStatus]] = frozenset(
    {
        ShipmentStatus.DELIVERED,
        ShipmentStatus.DELIVERY_FAILED,
        ShipmentStatus.RETURNED,
    }
)

# Fachlich echte Rueckschritte: Sie duerfen einen weiter fortgeschrittenen Status
# ueberschreiben, weil sie eine tatsaechliche Verschlechterung beschreiben.
REGRESSION_STATUSES: Final[frozenset[ShipmentStatus]] = frozenset(
    {
        ShipmentStatus.DELAYED,
        ShipmentStatus.DELIVERY_FAILED,
        ShipmentStatus.RETURNED,
    }
)


def ensure_aware(value: datetime, *, field_name: str = "Zeitstempel") -> datetime:
    """Einen Zeitstempel als UTC zurueckgeben; naive Werte sind ein Fehler."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} muss timezone-aware sein: {value!r}")
    return dt_util.as_utc(value)


def _dt_to_iso(value: datetime | None) -> str | None:
    """Zeitstempel als UTC-ISO-String, `None` bleibt `None`."""
    if value is None:
        return None
    return ensure_aware(value).isoformat()


def _dt_from_iso(value: Any, *, field_name: str = "Zeitstempel") -> datetime | None:
    """UTC-ISO-String zurueck in einen aware `datetime`."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_aware(value, field_name=field_name)
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        raise ValueError(f"{field_name} ist kein gueltiger Zeitstempel: {value!r}")
    return ensure_aware(parsed, field_name=field_name)


def _require_dt(value: Any, *, field_name: str) -> datetime:
    """Wie `_dt_from_iso`, aber der Wert ist Pflicht."""
    parsed = _dt_from_iso(value, field_name=field_name)
    if parsed is None:
        raise ValueError(f"{field_name} fehlt")
    return parsed


def _check_version(data: Mapping[str, Any], model: str) -> None:
    """Serialisierte Daten aus einer neueren Version ablehnen."""
    version = data.get("version", MODEL_VERSION)
    if not isinstance(version, int) or version > MODEL_VERSION:
        raise ValueError(f"{model}: unbekannte Schema-Version {version!r}")


def _str_list(value: Any) -> list[str]:
    """Eine Liste von Strings aus rohen Daten lesen (Einzelwert wird eingepackt)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


@dataclass(frozen=True, slots=True)
class ShipmentEvent:
    """Ein einzelner Scan-/Statuseintrag einer Sendung."""

    timestamp: datetime
    status: ShipmentStatus = ShipmentStatus.UNKNOWN
    text: str | None = None
    location: str | None = None

    def __post_init__(self) -> None:
        """Zeitstempel normalisieren (das Objekt ist frozen, daher ueber `object`)."""
        object.__setattr__(self, "timestamp", ensure_aware(self.timestamp, field_name="timestamp"))

    @property
    def dedup_key(self) -> tuple[datetime, ShipmentStatus]:
        """Identitaet eines Events: gleicher Zeitpunkt und gleicher Status."""
        return (self.timestamp, self.status)

    @property
    def detail_score(self) -> int:
        """Wie viel Zusatzinfo das Event traegt — entscheidet Dubletten."""
        return len(self.text or "") + len(self.location or "")

    def to_dict(self) -> dict[str, Any]:
        """Serialisieren."""
        return {
            "version": MODEL_VERSION,
            "timestamp": _dt_to_iso(self.timestamp),
            "status": self.status.value,
            "text": self.text,
            "location": self.location,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Deserialisieren."""
        _check_version(data, "ShipmentEvent")
        return cls(
            timestamp=_require_dt(data.get("timestamp"), field_name="ShipmentEvent.timestamp"),
            status=ShipmentStatus(data.get("status", ShipmentStatus.UNKNOWN)),
            text=data.get("text"),
            location=data.get("location"),
        )


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Nachweis, welche Quelle wann etwas zu einer Sendung beigetragen hat."""

    source_id: str
    fetched_at: datetime
    reference: str | None = None
    """Referenz auf die Rohdaten, z.B. IMAP-Message-ID oder API-Antwort-ID."""

    def __post_init__(self) -> None:
        """Zeitstempel normalisieren."""
        object.__setattr__(
            self, "fetched_at", ensure_aware(self.fetched_at, field_name="fetched_at")
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisieren."""
        return {
            "version": MODEL_VERSION,
            "source_id": self.source_id,
            "fetched_at": _dt_to_iso(self.fetched_at),
            "reference": self.reference,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Deserialisieren."""
        _check_version(data, "SourceRecord")
        return cls(
            source_id=str(data["source_id"]),
            fetched_at=_require_dt(data.get("fetched_at"), field_name="SourceRecord.fetched_at"),
            reference=data.get("reference"),
        )


@dataclass(slots=True)
class Shipment:
    """Eine Sendung — das zentrale Objekt der Integration."""

    id: str
    carrier: str
    tracking_number: str | None = None
    status: ShipmentStatus = ShipmentStatus.UNKNOWN
    status_text: str | None = None
    """Originaltext des Zustellers, unuebersetzt."""
    eta_start: datetime | None = None
    eta_end: datetime | None = None
    delivered_at: datetime | None = None
    destination_id: str | None = None
    destination_raw: str | None = None
    """Adresse so, wie sie in der Mail stand — Grundlage der Zuordnung in Step 1.5."""
    sender: str | None = None
    description: str | None = None
    order_number: str | None = None
    pickup_location: str | None = None
    pickup_code: str | None = None
    tracking_url: str | None = None
    events: list[ShipmentEvent] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    first_seen: datetime = field(default_factory=dt_util.utcnow)
    last_updated: datetime = field(default_factory=dt_util.utcnow)

    def __post_init__(self) -> None:
        """Status- und Zeitfelder normalisieren."""
        self.status = ShipmentStatus(self.status)
        self.first_seen = ensure_aware(self.first_seen, field_name="first_seen")
        self.last_updated = ensure_aware(self.last_updated, field_name="last_updated")
        for name in ("eta_start", "eta_end", "delivered_at"):
            value: datetime | None = getattr(self, name)
            if value is not None:
                setattr(self, name, ensure_aware(value, field_name=name))

    @property
    def is_closed(self) -> bool:
        """True, wenn die Sendung abgeschlossen ist."""
        return self.status.is_closed

    @property
    def is_active(self) -> bool:
        """True, solange die Sendung unterwegs ist."""
        return not self.is_closed

    @property
    def closed_at(self) -> datetime:
        """Zeitpunkt, ab dem die Retention laeuft."""
        return self.delivered_at or self.last_updated

    @property
    def source_ids(self) -> list[str]:
        """IDs aller Quellen, die zu dieser Sendung beigetragen haben."""
        return list(dict.fromkeys(record.source_id for record in self.sources))

    def as_summary(self) -> dict[str, Any]:
        """Kompakte Darstellung fuer die Sensor-Attributliste (ohne Event-Historie)."""
        return {
            "id": self.id,
            "carrier": self.carrier,
            "tracking_number": self.tracking_number,
            "status": self.status.value,
            "status_text": self.status_text,
            "eta_start": _dt_to_iso(self.eta_start),
            "eta_end": _dt_to_iso(self.eta_end),
            "delivered_at": _dt_to_iso(self.delivered_at),
            "destination_id": self.destination_id,
            "sender": self.sender,
            "description": self.description,
            "pickup_location": self.pickup_location,
            "tracking_url": self.tracking_url,
            "last_updated": _dt_to_iso(self.last_updated),
        }

    def to_dict(self) -> dict[str, Any]:
        """Vollstaendig serialisieren."""
        return {
            "version": MODEL_VERSION,
            "id": self.id,
            "carrier": self.carrier,
            "tracking_number": self.tracking_number,
            "status": self.status.value,
            "status_text": self.status_text,
            "eta_start": _dt_to_iso(self.eta_start),
            "eta_end": _dt_to_iso(self.eta_end),
            "delivered_at": _dt_to_iso(self.delivered_at),
            "destination_id": self.destination_id,
            "destination_raw": self.destination_raw,
            "sender": self.sender,
            "description": self.description,
            "order_number": self.order_number,
            "pickup_location": self.pickup_location,
            "pickup_code": self.pickup_code,
            "tracking_url": self.tracking_url,
            "events": [event.to_dict() for event in self.events],
            "sources": [record.to_dict() for record in self.sources],
            "first_seen": _dt_to_iso(self.first_seen),
            "last_updated": _dt_to_iso(self.last_updated),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Deserialisieren."""
        _check_version(data, "Shipment")
        return cls(
            id=str(data["id"]),
            carrier=str(data["carrier"]),
            tracking_number=data.get("tracking_number"),
            status=ShipmentStatus(data.get("status", ShipmentStatus.UNKNOWN)),
            status_text=data.get("status_text"),
            eta_start=_dt_from_iso(data.get("eta_start"), field_name="eta_start"),
            eta_end=_dt_from_iso(data.get("eta_end"), field_name="eta_end"),
            delivered_at=_dt_from_iso(data.get("delivered_at"), field_name="delivered_at"),
            destination_id=data.get("destination_id"),
            destination_raw=data.get("destination_raw"),
            sender=data.get("sender"),
            description=data.get("description"),
            order_number=data.get("order_number"),
            pickup_location=data.get("pickup_location"),
            pickup_code=data.get("pickup_code"),
            tracking_url=data.get("tracking_url"),
            events=[ShipmentEvent.from_dict(item) for item in data.get("events") or []],
            sources=[SourceRecord.from_dict(item) for item in data.get("sources") or []],
            first_seen=_require_dt(data.get("first_seen"), field_name="first_seen"),
            last_updated=_require_dt(data.get("last_updated"), field_name="last_updated"),
        )


@dataclass(slots=True)
class Destination:
    """Ein manuell konfigurierter Zielort mit seinen Match-Regeln.

    Die eigentliche Zuordnung Sendung -> Zielort entsteht in Step 1.5; hier steht
    nur der Datencontainer inklusive Serialisierung.
    """

    id: str
    name: str
    recipient_names: list[str] = field(default_factory=list)
    streets: list[str] = field(default_factory=list)
    postal_codes: list[str] = field(default_factory=list)
    packstations: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialisieren."""
        return {
            "version": MODEL_VERSION,
            "id": self.id,
            "name": self.name,
            "recipient_names": list(self.recipient_names),
            "streets": list(self.streets),
            "postal_codes": list(self.postal_codes),
            "packstations": list(self.packstations),
            "emails": list(self.emails),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Deserialisieren."""
        _check_version(data, "Destination")
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            recipient_names=_str_list(data.get("recipient_names")),
            streets=_str_list(data.get("streets")),
            postal_codes=_str_list(data.get("postal_codes")),
            packstations=_str_list(data.get("packstations")),
            emails=_str_list(data.get("emails")),
        )
