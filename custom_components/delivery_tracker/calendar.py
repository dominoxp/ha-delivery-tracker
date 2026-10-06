"""Kalender: erwartete und zugestellte Lieferungen als Termine.

Eine Entity am Haupt-Device; jede Sendung mit Termin ist ein Ereignis:

- bekanntes Zeitfenster → Termin von `eta_start` bis `eta_end`, mindestens
  eine Stunde lang („gegen 14 Uhr" → 14 bis 15 Uhr);
- nur ein Datum (`parse_eta` legt es als Mitternacht bis Mitternacht ab)
  → ganztaegiger Termin, als Datum ohne Uhrzeit;
- kein Termin → kein Ereignis.

Eine zugestellte Sendung wandert auf den Zeitpunkt der Zustellung und bleibt
dort als vergangener Termin stehen, bis die Retention sie aus dem Store nimmt.
Die `uid` ist die Sendungs-ID: Ein neuer Zustelltermin verschiebt also den
bestehenden Termin, statt einen zweiten anzulegen.

Der Titel landet in Automationen und Benachrichtigungen und bleibt deshalb
kurz („DHL: Shop → Zuhause"); Trackingnummer und Status stehen in der
Beschreibung.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DESTINATION_UNKNOWN
from .coordinator import DeliveryTrackerCoordinator
from .dates import whole_local_days
from .entity import DeliveryTrackerEntity
from .entry import DeliveryTrackerConfigEntry
from .models import Destination, Shipment, ShipmentStatus

# Der Coordinator rechnet zentral; die Entity liest nur.
PARALLEL_UPDATES = 0

# Kuerzester Termin mit Uhrzeit; kuerzere Fenster werden nach hinten verlaengert.
MIN_EVENT_DURATION = timedelta(hours=1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeliveryTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Den Kalender der erwarteten Lieferungen anlegen."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            ExpectedDeliveriesCalendar(
                runtime.coordinator,
                carrier_names={
                    plugin.carrier_id: plugin.name for plugin in runtime.registry.plugins
                },
                destinations=runtime.destinations,
            )
        ]
    )


def shipment_event(
    shipment: Shipment,
    *,
    carrier_name: str,
    destination: Destination | None = None,
) -> CalendarEvent | None:
    """Eine Sendung als Kalenderereignis; ohne Termin `None`."""
    span = _event_span(shipment)
    if span is None:
        return None
    start, end = span
    return CalendarEvent(
        start=start,
        end=end,
        summary=_summary(shipment, carrier_name, destination),
        description=_description(shipment),
        location=shipment.destination_raw or (destination.name if destination else None),
        uid=shipment.id,
    )


def _event_span(shipment: Shipment) -> tuple[date, date] | tuple[datetime, datetime] | None:
    """Beginn und Ende des Termins: Datumswerte fuer ganze Tage, sonst Ortszeit."""
    end: datetime | None
    if shipment.status is ShipmentStatus.DELIVERED and shipment.delivered_at is not None:
        start, end = shipment.delivered_at, None
        # Eine Zustellung ohne Uhrzeit steht als Mitternacht im Modell.
        if dt_util.as_local(start).time() == time.min:
            day = dt_util.as_local(start).date()
            return day, day + timedelta(days=1)
    elif shipment.eta_start is not None:
        start, end = shipment.eta_start, shipment.eta_end
    else:
        return None

    # Datumswerte ohne Uhrzeit, sonst rutscht ein ganztaegiger Termin je nach
    # Zeitzone der Anzeige um einen Tag.
    if (days := whole_local_days(start, end)) is not None:
        return days
    # Mindestens eine Stunde: „gegen 14 Uhr" und eine Zustellung um 15:30 sind
    # Zeitpunkte, ein Termin ohne Dauer waere aber nie „an" und in der Karte
    # kaum zu sehen.
    start_local = dt_util.as_local(start)
    end_local = dt_util.as_local(end) if end is not None else start_local
    return start_local, max(start_local + MIN_EVENT_DURATION, end_local)


def _summary(shipment: Shipment, carrier_name: str, destination: Destination | None) -> str:
    """„Zusteller: Absender → Zielort", fehlende Teile entfallen."""
    summary = carrier_name
    if shipment.sender and shipment.sender.casefold() != carrier_name.casefold():
        summary = f"{summary}: {shipment.sender}"
    if destination is not None:
        summary = f"{summary} → {destination.name}"
    return summary


def _description(shipment: Shipment) -> str | None:
    """Sendungsnummer, Status und Abholort — je eine Zeile, soweit bekannt."""
    lines = []
    if shipment.tracking_number:
        lines.append(f"Sendungsnummer: {shipment.tracking_number}")
    lines.append(f"Status: {shipment.status_text or shipment.status.value}")
    if shipment.pickup_location:
        lines.append(f"Abholort: {shipment.pickup_location}")
    return "\n".join(lines)


class ExpectedDeliveriesCalendar(DeliveryTrackerEntity, CalendarEntity):
    """Alle Sendungen mit Termin, inklusive der zugestellten in der Retention."""

    _attr_translation_key = "expected_deliveries"

    def __init__(
        self,
        coordinator: DeliveryTrackerCoordinator,
        *,
        carrier_names: dict[str, str],
        destinations: list[Destination],
    ) -> None:
        """Kalender am Haupt-Device anlegen.

        Zusteller- und Zielortnamen aendern sich nur mit einem Reload des Entries.
        """
        super().__init__(coordinator, "expected_deliveries")
        self._carrier_names = carrier_names
        self._destinations = {destination.id: destination for destination in destinations}

    def _event_for(self, shipment: Shipment) -> CalendarEvent | None:
        """Ereignis einer Sendung mit den Namen aus der Konfiguration."""
        destination = None
        if shipment.destination_id not in (None, DESTINATION_UNKNOWN):
            destination = self._destinations.get(shipment.destination_id)
        return shipment_event(
            shipment,
            carrier_name=self._carrier_names.get(shipment.carrier, shipment.carrier),
            destination=destination,
        )

    def _events(self, shipments: list[Shipment]) -> list[CalendarEvent]:
        """Ereignisse der Sendungen, nach Beginn sortiert."""
        events = [event for shipment in shipments if (event := self._event_for(shipment))]
        return sorted(events, key=lambda event: (event.start_datetime_local, event.uid or ""))

    @property
    def event(self) -> CalendarEvent | None:
        """Laufende oder naechste Lieferung; abgeschlossene Sendungen zaehlen nicht."""
        now = dt_util.now()
        for event in self._events(self.coordinator.data.active):
            if event.end_datetime_local > now:
                return event
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Alle Termine, die den Zeitraum beruehren — auch vergangene Zustellungen."""
        return [
            event
            for event in self._events(self.coordinator.data.shipments)
            if event.start_datetime_local < end_date and event.end_datetime_local > start_date
        ]
