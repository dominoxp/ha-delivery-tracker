/**
 * Filter, Sortierung, Obergrenze und Gruppierung der Sendungsliste.
 *
 * Reine Funktionen ohne DOM, damit sie sich einzeln testen lassen. Die
 * Reihenfolge der Schritte: filtern, sortieren, kuerzen, gruppieren — die
 * Obergrenze gilt fuer die ganze Karte, nicht je Gruppe.
 */
import {
  DEFAULT_GROUP_BY,
  DEFAULT_SORT_BY,
  type DeliveryTrackerCardConfig,
  type GroupBy,
  type SortBy,
} from "./config";
import { DESTINATION_UNKNOWN, destinationOf, type Shipment, type ShipmentStatus } from "./shipment";

/** Anzeigenamen fuer IDs; die Karte fuellt sie aus den Sensor-Attributen. */
export interface Labels {
  carrier(id: string): string;
  destination(id: string): string;
  status(status: ShipmentStatus): string;
  /** Konfigurierte Zielorte in ihrer Reihenfolge — bestimmt die Gruppenfolge. */
  destinationOrder: readonly string[];
}

export interface ShipmentGroup {
  /** ID des Zielorts, Status oder Zusteller; leer ohne Gruppierung. */
  key: string;
  label?: string;
  shipments: Shipment[];
}

export interface ShipmentView {
  groups: ShipmentGroup[];
  /** Sendungen nach dem Filter, vor der Obergrenze. */
  matching: number;
  /** Wegen der Obergrenze ausgeblendete Sendungen. */
  hidden: number;
}

/**
 * Reihenfolge fuer „nach Status": was heute Aufmerksamkeit braucht zuerst
 * (abholen, gleich da, Probleme), Abgeschlossenes zuletzt.
 */
export const STATUS_ORDER: readonly ShipmentStatus[] = [
  "ready_for_pickup",
  "out_for_delivery",
  "delivery_failed",
  "delayed",
  "customs",
  "in_transit",
  "label_created",
  "ordered",
  "unknown",
  "returned",
  "delivered",
];

/** Alle Filter wirken zusammen (UND); ein fehlender Filter laesst alles durch. */
export function filterShipments(
  shipments: readonly Shipment[],
  config: Pick<DeliveryTrackerCardConfig, "destinations" | "statuses" | "carriers">,
): Shipment[] {
  const { destinations, statuses, carriers } = config;
  return shipments.filter(
    (shipment) =>
      (!destinations?.length || destinations.includes(destinationOf(shipment))) &&
      (!statuses?.length || statuses.includes(shipment.status)) &&
      (!carriers?.length || carriers.includes(shipment.carrier)),
  );
}

function time(value: string | undefined): number | undefined {
  if (!value) {
    return undefined;
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? undefined : parsed;
}

/**
 * Wie die Integration (`coordinator.shipment_sort_key`): mit Termin nach
 * Beginn, die naechste zuerst; danach die ohne Termin, juengste Aenderung zuerst.
 */
function compareEta(a: Shipment, b: Shipment): number {
  const etaA = time(a.eta_start);
  const etaB = time(b.eta_start);
  if (etaA !== undefined && etaB !== undefined) {
    return etaA - etaB;
  }
  if (etaA !== undefined || etaB !== undefined) {
    return etaA !== undefined ? -1 : 1;
  }
  return compareUpdated(a, b);
}

/** Juengste Aenderung zuerst, ohne Zeitstempel zuletzt. */
function compareUpdated(a: Shipment, b: Shipment): number {
  return (time(b.last_updated) ?? -Infinity) - (time(a.last_updated) ?? -Infinity);
}

function compareStatus(a: Shipment, b: Shipment): number {
  return STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status);
}

export function sortShipments(
  shipments: readonly Shipment[],
  sortBy: SortBy,
  labels: Labels,
): Shipment[] {
  const primary: Record<SortBy, (a: Shipment, b: Shipment) => number> = {
    eta: compareEta,
    status: compareStatus,
    last_updated: compareUpdated,
    carrier: (a, b) => labels.carrier(a.carrier).localeCompare(labels.carrier(b.carrier)),
  };
  const compare = primary[sortBy];
  // Gleichstaende: Termin, dann ID — die Liste springt nicht bei jedem Update.
  return [...shipments].sort(
    (a, b) => compare(a, b) || compareEta(a, b) || a.id.localeCompare(b.id),
  );
}

/** Schluessel einer Sendung je Gruppierung. */
function groupKey(shipment: Shipment, groupBy: Exclude<GroupBy, "none">): string {
  switch (groupBy) {
    case "destination":
      return destinationOf(shipment);
    case "status":
      return shipment.status;
    case "carrier":
      return shipment.carrier;
  }
}

/** Reihenfolge der Gruppen; innerhalb einer Gruppe bleibt die Sortierung. */
function groupRank(key: string, groupBy: Exclude<GroupBy, "none">, labels: Labels): number {
  if (groupBy === "status") {
    return STATUS_ORDER.indexOf(key as ShipmentStatus);
  }
  if (groupBy === "destination") {
    if (key === DESTINATION_UNKNOWN) {
      return Number.MAX_SAFE_INTEGER;
    }
    const index = labels.destinationOrder.indexOf(key);
    // Nicht (mehr) konfigurierte Zielorte nach den bekannten.
    return index === -1 ? labels.destinationOrder.length : index;
  }
  return 0;
}

function groupLabel(key: string, groupBy: Exclude<GroupBy, "none">, labels: Labels): string {
  switch (groupBy) {
    case "destination":
      return labels.destination(key);
    case "status":
      return labels.status(key as ShipmentStatus);
    case "carrier":
      return labels.carrier(key);
  }
}

export function groupShipments(
  shipments: readonly Shipment[],
  groupBy: GroupBy,
  labels: Labels,
): ShipmentGroup[] {
  if (groupBy === "none") {
    return shipments.length ? [{ key: "", shipments: [...shipments] }] : [];
  }
  const groups = new Map<string, ShipmentGroup>();
  for (const shipment of shipments) {
    const key = groupKey(shipment, groupBy);
    let group = groups.get(key);
    if (!group) {
      group = { key, label: groupLabel(key, groupBy, labels), shipments: [] };
      groups.set(key, group);
    }
    group.shipments.push(shipment);
  }
  return [...groups.values()].sort(
    (a, b) =>
      groupRank(a.key, groupBy, labels) - groupRank(b.key, groupBy, labels) ||
      (a.label ?? "").localeCompare(b.label ?? ""),
  );
}

/** Der ganze Weg von der Attributliste zur Anzeige. */
export function buildView(
  shipments: readonly Shipment[],
  config: DeliveryTrackerCardConfig,
  labels: Labels,
): ShipmentView {
  const matching = filterShipments(shipments, config);
  const sorted = sortShipments(matching, config.sort_by ?? DEFAULT_SORT_BY, labels);
  const shown = config.max_items ? sorted.slice(0, config.max_items) : sorted;
  return {
    groups: groupShipments(shown, config.group_by ?? DEFAULT_GROUP_BY, labels),
    matching: matching.length,
    hidden: sorted.length - shown.length,
  };
}
