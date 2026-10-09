import { describe, expect, it } from "vitest";

import type { DeliveryTrackerCardConfig } from "../src/config";
import { makeLabels } from "../src/labels";
import { buildView, filterShipments, groupShipments, sortShipments } from "../src/list";
import { parseShipments, type Shipment } from "../src/shipment";
import { ENTITY, sampleShipments, shipmentSensor } from "./fixtures";

const labels = makeLabels(shipmentSensor(), "de");
const shipments = parseShipments(sampleShipments());
const ids = (list: readonly Shipment[]) => list.map((shipment) => shipment.id);
const config = (extra: Partial<DeliveryTrackerCardConfig> = {}): DeliveryTrackerCardConfig => ({
  type: "custom:delivery-tracker-card",
  entity: ENTITY,
  ...extra,
});

describe("parseShipments", () => {
  it("liest das Attribut und verwirft Unbrauchbares", () => {
    const parsed = parseShipments([
      ...sampleShipments(),
      null,
      "kaputt",
      { carrier: "dhl" },
      { id: "neu", status: "teleportiert" },
    ]);
    expect(ids(parsed)).toEqual([
      "dhl_heute",
      "dpd_morgen",
      "amazon_locker",
      "hermes_retour",
      "neu",
    ]);
    // Unbekannter Status und fehlender Zusteller bekommen sichere Werte.
    expect(parsed[4]).toEqual({ id: "neu", carrier: "generic", status: "unknown" });
    // `null` aus Python wird zu `undefined`.
    expect(parsed[0].delivered_at).toBeUndefined();
  });

  it("ist ohne Liste leer", () => {
    expect(parseShipments(undefined)).toEqual([]);
    expect(parseShipments({})).toEqual([]);
  });
});

describe("Filter", () => {
  it("ohne Filter bleibt alles", () => {
    expect(ids(filterShipments(shipments, {}))).toEqual(ids(shipments));
  });

  it.each([
    [{ destinations: ["home"] }, ["dhl_heute", "hermes_retour"]],
    [{ destinations: ["unknown"] }, ["amazon_locker"]],
    [{ statuses: ["delayed", "returned"] }, ["dpd_morgen", "hermes_retour"]],
    [{ carriers: ["amazon"] }, ["amazon_locker"]],
  ] as const)("%j wirkt einzeln", (filter, expected) => {
    expect(ids(filterShipments(shipments, filter as never))).toEqual(expected);
  });

  it("wirkt kombiniert als UND", () => {
    const filter = { destinations: ["home"], statuses: ["returned" as const] };
    expect(ids(filterShipments(shipments, filter))).toEqual(["hermes_retour"]);
    expect(ids(filterShipments(shipments, { ...filter, carriers: ["dhl"] }))).toEqual([]);
  });

  it("ordnet Sendungen ohne Zielort dem unbekannten zu", () => {
    const [ohne] = parseShipments([{ id: "x", carrier: "dhl", status: "in_transit" }]);
    expect(filterShipments([ohne], { destinations: ["unknown"] })).toHaveLength(1);
  });
});

describe("Sortierung", () => {
  it("nach Termin wie die Integration: mit Termin zuerst, dann juengste Aenderung", () => {
    const reversed = [...shipments].reverse();
    expect(ids(sortShipments(reversed, "eta", labels))).toEqual([
      "dhl_heute",
      "dpd_morgen",
      "amazon_locker",
      "hermes_retour",
    ]);
  });

  it("nach Status: Handlungsbedarf zuerst, Abgeschlossenes zuletzt", () => {
    expect(ids(sortShipments(shipments, "status", labels))).toEqual([
      "amazon_locker",
      "dhl_heute",
      "dpd_morgen",
      "hermes_retour",
    ]);
  });

  it("nach letzter Aktualisierung: juengste zuerst", () => {
    expect(ids(sortShipments(shipments, "last_updated", labels))).toEqual([
      "amazon_locker",
      "dhl_heute",
      "dpd_morgen",
      "hermes_retour",
    ]);
  });

  it("nach Zusteller: alphabetisch nach Anzeigename", () => {
    expect(ids(sortShipments(shipments, "carrier", labels))).toEqual([
      "amazon_locker",
      "dhl_heute",
      "dpd_morgen",
      "hermes_retour",
    ]);
  });

  it("veraendert die Eingabe nicht", () => {
    const before = ids(shipments);
    sortShipments(shipments, "status", labels);
    expect(ids(shipments)).toEqual(before);
  });
});

describe("Gruppierung", () => {
  it("ohne Gruppierung eine Gruppe ohne Ueberschrift", () => {
    const groups = groupShipments(shipments, "none", labels);
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBeUndefined();
    expect(groupShipments([], "none", labels)).toEqual([]);
  });

  it("nach Zielort: beschriftet, in Konfigurationsreihenfolge, Unbekannt zuletzt", () => {
    const groups = groupShipments(shipments, "destination", labels);
    expect(groups.map((group) => [group.label, ids(group.shipments)])).toEqual([
      ["Zuhause", ["dhl_heute", "hermes_retour"]],
      ["Büro", ["dpd_morgen"]],
      ["Unbekannter Zielort", ["amazon_locker"]],
    ]);
  });

  it("nach Status: beschriftet in Statusreihenfolge", () => {
    const groups = groupShipments(shipments, "status", labels);
    expect(groups.map((group) => group.label)).toEqual([
      "Abholbereit",
      "In Zustellung",
      "Verzögert",
      "Zurück an Absender",
    ]);
  });

  it("nach Zusteller: mit Namen aus dem Sensor", () => {
    const groups = groupShipments(shipments, "carrier", labels);
    expect(groups.map((group) => group.label)).toEqual(["Amazon", "DHL Paket", "DPD", "Hermes"]);
  });
});

describe("buildView", () => {
  it("filtert, sortiert, kuerzt und gruppiert in dieser Reihenfolge", () => {
    const view = buildView(
      shipments,
      config({ statuses: ["out_for_delivery", "delayed", "returned"], max_items: 2 }),
      labels,
    );
    expect(view.matching).toBe(3);
    expect(view.hidden).toBe(1);
    expect(view.groups.flatMap((group) => ids(group.shipments))).toEqual([
      "dhl_heute",
      "dpd_morgen",
    ]);
  });

  it("die Obergrenze gilt fuer die ganze Karte, nicht je Gruppe", () => {
    const view = buildView(shipments, config({ group_by: "destination", max_items: 3 }), labels);
    expect(view.groups.map((group) => [group.key, ids(group.shipments)])).toEqual([
      ["home", ["dhl_heute"]],
      ["buero", ["dpd_morgen"]],
      ["unknown", ["amazon_locker"]],
    ]);
  });
});
