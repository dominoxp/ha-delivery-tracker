/**
 * Anzeigenamen fuer Zusteller, Zielorte und Status.
 *
 * Die Sendungsliste traegt nur IDs; die Namen stehen in den Attributen
 * `carriers` und `destinations` desselben Sensors. Fehlt ein Name (etwa ein
 * inzwischen abgeschalteter Zusteller), hilft eine eingebaute Liste, zuletzt
 * die ID selbst.
 */
import type { Labels } from "./list";
import { localize } from "./localize";
import { DESTINATION_UNKNOWN, type ShipmentStatus } from "./shipment";
import type { HassEntity } from "./types";

const CARRIER_NAMES: Record<string, string> = {
  dhl: "DHL",
  amazon: "Amazon",
  dpd: "DPD",
  gls: "GLS",
  hermes: "Hermes",
  ups: "UPS",
};

/** Ein Attribut `{id: name}` lesen; alles andere ergibt eine leere Zuordnung. */
export function nameMap(value: unknown): Record<string, string> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(value).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );
}

export function makeLabels(stateObj: HassEntity | undefined, language?: string): Labels {
  const carriers = nameMap(stateObj?.attributes.carriers);
  const destinations = nameMap(stateObj?.attributes.destinations);
  return {
    carrier: (id) =>
      carriers[id] ??
      CARRIER_NAMES[id] ??
      (id === "generic" ? localize(language, "carrier.generic") : id),
    destination: (id) =>
      id === DESTINATION_UNKNOWN
        ? localize(language, "destination.unknown")
        : (destinations[id] ?? id),
    status: (status: ShipmentStatus) => localize(language, `status.${status}`),
    destinationOrder: Object.keys(destinations),
  };
}
