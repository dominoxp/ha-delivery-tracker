import type { HassEntity, HomeAssistant } from "../src/types";

export const ENTITY = "sensor.delivery_tracker_aktive_sendungen";
export const TIME_ZONE = "Europe/Berlin";
/** Dienstag, 6. Oktober 2026, 12:00 in Berlin. */
export const NOW = new Date("2026-10-06T10:00:00Z");

/** Sendungen im Format von `Shipment.as_summary()`, in der Reihenfolge des Sensors. */
export function sampleShipments(): Record<string, unknown>[] {
  return [
    {
      id: "dhl_heute",
      carrier: "dhl",
      tracking_number: "00340434161094042557",
      status: "out_for_delivery",
      status_text: "Die Sendung wurde in das Zustellfahrzeug geladen.",
      // Heute 14 bis 18 Uhr in Berlin.
      eta_start: "2026-10-06T12:00:00+00:00",
      eta_end: "2026-10-06T16:00:00+00:00",
      delivered_at: null,
      destination_id: "home",
      sender: "Fahrradladen",
      description: "Fahrradschlauch",
      pickup_location: null,
      pickup_code: null,
      tracking_url: "https://www.dhl.de/de/privatkunden.html?piececode=00340434161094042557",
      last_updated: "2026-10-06T07:00:00+00:00",
    },
    {
      id: "dpd_morgen",
      carrier: "dpd",
      tracking_number: "09981122334455",
      status: "delayed",
      // Morgen, ganztaegig: Mitternacht bis Mitternacht in Berlin.
      eta_start: "2026-10-06T22:00:00+00:00",
      eta_end: "2026-10-07T22:00:00+00:00",
      destination_id: "buero",
      sender: "Werkzeughandel",
      description: "Akkuschrauber",
      tracking_url: "javascript:alert(1)",
      last_updated: "2026-10-05T18:00:00+00:00",
    },
    {
      id: "amazon_locker",
      carrier: "amazon",
      tracking_number: null,
      status: "ready_for_pickup",
      destination_id: "unknown",
      sender: "Amazon",
      description: "Buch (+2 weitere Artikel)",
      pickup_location: "Amazon Locker Hauptbahnhof",
      pickup_code: "482913",
      last_updated: "2026-10-06T08:00:00+00:00",
    },
    {
      id: "hermes_retour",
      carrier: "hermes",
      tracking_number: "H1001234567890123451",
      status: "returned",
      destination_id: "home",
      sender: "Modeversand",
      last_updated: "2026-10-04T12:00:00+00:00",
    },
  ];
}

export function entity(
  entityId: string,
  attributes: Record<string, unknown>,
  state = "0",
): HassEntity {
  return {
    entity_id: entityId,
    state,
    attributes,
    last_changed: "2026-10-06T08:00:00+00:00",
    last_updated: "2026-10-06T08:00:00+00:00",
  };
}

/** Der Sensor mit Beispieldaten und Namen, wie ihn die Integration schreibt. */
export function shipmentSensor(
  shipments: Record<string, unknown>[] = sampleShipments(),
  extra: Record<string, unknown> = {},
): HassEntity {
  return entity(
    ENTITY,
    {
      friendly_name: "Aktive Sendungen",
      shipments,
      carriers: { dhl: "DHL Paket", dpd: "DPD", amazon: "Amazon", hermes: "Hermes" },
      destinations: { home: "Zuhause", buero: "Büro" },
      ...extra,
    },
    String(shipments.length),
  );
}

export function hass(...entities: HassEntity[]): HomeAssistant {
  return {
    states: Object.fromEntries(entities.map((e) => [e.entity_id, e])),
    locale: { language: "de" },
    config: { time_zone: TIME_ZONE },
  };
}

/** Text ohne Formatierungs-Leerraum. `\s` erfasst auch die schmalen Leerzeichen aus `Intl`. */
export function normalize(value: string | null | undefined): string {
  return (value ?? "").replace(/\s+/g, " ").trim();
}
