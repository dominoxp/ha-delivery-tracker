/**
 * Die Sendungen aus dem Attribut `shipments` eines Sensors der Integration.
 *
 * Das Format ist `Shipment.as_summary()` aus `models.py`. Die Karte liest nur
 * dieses Attribut; was dort nicht steht, kann sie nicht zeigen.
 */

/** Gleiche Werte und Reihenfolge wie `ShipmentStatus` in `models.py`. */
export const STATUSES = [
  "unknown",
  "ordered",
  "label_created",
  "in_transit",
  "delayed",
  "customs",
  "out_for_delivery",
  "ready_for_pickup",
  "delivered",
  "delivery_failed",
  "returned",
] as const;

export type ShipmentStatus = (typeof STATUSES)[number];

/** Eingebauter Zielort der Integration fuer nicht zugeordnete Sendungen. */
export const DESTINATION_UNKNOWN = "unknown";

export interface Shipment {
  id: string;
  carrier: string;
  status: ShipmentStatus;
  tracking_number?: string;
  status_text?: string;
  eta_start?: string;
  eta_end?: string;
  delivered_at?: string;
  destination_id?: string;
  sender?: string;
  description?: string;
  pickup_location?: string;
  pickup_code?: string;
  tracking_url?: string;
  last_updated?: string;
}

const TEXT_FIELDS = [
  "tracking_number",
  "status_text",
  "eta_start",
  "eta_end",
  "delivered_at",
  "destination_id",
  "sender",
  "description",
  "pickup_location",
  "pickup_code",
  "tracking_url",
  "last_updated",
] as const;

export function isStatus(value: unknown): value is ShipmentStatus {
  return typeof value === "string" && (STATUSES as readonly string[]).includes(value);
}

/**
 * Die Liste aus dem Attribut lesen. Unbrauchbare Eintraege fallen weg, leere
 * Felder werden zu `undefined`, ein unbekannter Status zu `unknown` — eine
 * neuere Integration darf die Karte nicht zum Absturz bringen.
 */
export function parseShipments(raw: unknown): Shipment[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  const shipments: Shipment[] = [];
  for (const item of raw) {
    if (typeof item !== "object" || item === null) {
      continue;
    }
    const data = item as Record<string, unknown>;
    if (typeof data.id !== "string" || data.id === "") {
      continue;
    }
    const shipment: Shipment = {
      id: data.id,
      carrier: typeof data.carrier === "string" && data.carrier ? data.carrier : "generic",
      status: isStatus(data.status) ? data.status : "unknown",
    };
    for (const field of TEXT_FIELDS) {
      const value = data[field];
      if (typeof value === "string" && value !== "") {
        shipment[field] = value;
      }
    }
    shipments.push(shipment);
  }
  return shipments;
}

/** Zielort einer Sendung; ohne Zuordnung der eingebaute „unbekannt". */
export function destinationOf(shipment: Shipment): string {
  return shipment.destination_id ?? DESTINATION_UNKNOWN;
}

/** Nur http(s)-Links werden angeboten; alles andere koennte Code ausfuehren. */
export function safeTrackingUrl(shipment: Shipment): string | undefined {
  const url = shipment.tracking_url;
  if (!url) {
    return undefined;
  }
  try {
    const { protocol } = new URL(url);
    return protocol === "https:" || protocol === "http:" ? url : undefined;
  } catch {
    return undefined;
  }
}

// ----------------------------------------------------------------------
// Fortschritt
// ----------------------------------------------------------------------

/**
 * Die Stufen des normalen Verlaufs: bestellt, versandbereit, unterwegs,
 * in Zustellung (bzw. abholbereit), zugestellt.
 */
export const PROGRESS_STEPS = 5;

export type ProgressTone = "normal" | "warning" | "done" | "exception" | "unknown";

export interface Progress {
  /** Erreichte Stufe, 0 bis `PROGRESS_STEPS`. */
  step: number;
  tone: ProgressTone;
}

/**
 * Abbildung der Status auf die Stufen. Verzoegerung und Zoll liegen auf der
 * Stufe „unterwegs", die Verzoegerung als Warnung. Retour und gescheiterte
 * Zustellung sind kein Fortschritt: Sie werden als Ausnahme dargestellt.
 */
const PROGRESS: Record<ShipmentStatus, Progress> = {
  unknown: { step: 0, tone: "unknown" },
  ordered: { step: 1, tone: "normal" },
  label_created: { step: 2, tone: "normal" },
  in_transit: { step: 3, tone: "normal" },
  customs: { step: 3, tone: "normal" },
  delayed: { step: 3, tone: "warning" },
  out_for_delivery: { step: 4, tone: "normal" },
  ready_for_pickup: { step: 4, tone: "normal" },
  delivered: { step: 5, tone: "done" },
  delivery_failed: { step: 0, tone: "exception" },
  returned: { step: 0, tone: "exception" },
};

export function progressOf(status: ShipmentStatus): Progress {
  return PROGRESS[status];
}
