import { describe, expect, it } from "vitest";

import { formatDay, formatEta, formatTimestamp, type FormatContext } from "../src/format";
import type { Shipment } from "../src/shipment";
import { NOW, TIME_ZONE, normalize } from "./fixtures";

const de: FormatContext = { now: NOW, language: "de", timeZone: TIME_ZONE };
const en: FormatContext = { ...de, language: "en" };

function shipment(extra: Partial<Shipment>): Shipment {
  return { id: "x", carrier: "dhl", status: "in_transit", ...extra };
}

function eta(extra: Partial<Shipment>, ctx: FormatContext = de) {
  const label = formatEta(shipment(extra), ctx);
  return label && { kind: label.kind, text: normalize(label.text) };
}

/** Ein ganzer Tag in Berlin (Sommerzeit): Mitternacht bis Mitternacht. */
function berlinDay(isoDate: string): Partial<Shipment> {
  const start = new Date(`${isoDate}T00:00:00+02:00`);
  const end = new Date(start.getTime() + 86_400_000);
  return { eta_start: start.toISOString(), eta_end: end.toISOString() };
}

describe("Tagesangaben", () => {
  it.each([
    ["2026-10-06T21:59:00Z", "heute"],
    ["2026-10-06T22:00:00Z", "morgen"],
    ["2026-10-05T12:00:00Z", "gestern"],
    ["2026-10-08T12:00:00Z", "Donnerstag"],
    ["2026-10-12T12:00:00Z", "Montag"],
  ])("%s ist %s", (iso, expected) => {
    expect(formatDay(new Date(iso), de)).toBe(expected);
  });

  it("weiter weg mit Datum, im naechsten Jahr mit Jahreszahl", () => {
    expect(normalize(formatDay(new Date("2026-10-20T12:00:00Z"), de))).toMatch(/^Di\.?, 20\. Okt/);
    expect(normalize(formatDay(new Date("2027-01-05T12:00:00Z"), de))).toMatch(/2027/);
  });

  it("in der Sprache des Nutzers", () => {
    expect(formatDay(new Date("2026-10-06T12:00:00Z"), en)).toBe("today");
    expect(formatDay(new Date("2026-10-07T12:00:00Z"), en)).toBe("tomorrow");
    expect(formatDay(new Date("2026-10-08T12:00:00Z"), en)).toBe("Thursday");
  });
});

describe("Erwartete Zustellung", () => {
  it("Zeitfenster mit Tag und Uhrzeiten", () => {
    expect(eta({ eta_start: "2026-10-06T12:00:00Z", eta_end: "2026-10-06T16:00:00Z" })).toEqual({
      kind: "window",
      text: "heute 14–18 Uhr",
    });
  });

  it("Minuten nur, wenn sie etwas aussagen", () => {
    const label = eta({ eta_start: "2026-10-06T12:30:00Z", eta_end: "2026-10-06T14:00:00Z" });
    expect(label?.text).toMatch(/^heute 14:30\s?–\s?16:00/);
  });

  it("Zeitpunkt ohne Ende", () => {
    expect(eta({ eta_start: "2026-10-07T07:00:00Z" })).toEqual({
      kind: "window",
      text: "morgen 09 Uhr",
    });
  });

  it("ganztaegig nur mit dem Tag — sichtbar anders als ein Zeitfenster", () => {
    expect(eta(berlinDay("2026-10-07"))).toEqual({ kind: "allday", text: "morgen" });
    expect(eta(berlinDay("2026-10-09"))).toEqual({ kind: "allday", text: "Freitag" });
  });

  it("ganztaegig ueber mehrere Tage", () => {
    const { eta_start } = berlinDay("2026-10-07");
    const { eta_end } = berlinDay("2026-10-08");
    expect(eta({ eta_start, eta_end })).toEqual({
      kind: "allday",
      text: "morgen – Donnerstag",
    });
  });

  it("Ganztaegigkeit haengt an der HA-Zeitzone, nicht an UTC", () => {
    // Mitternacht in Berlin ist 22 Uhr in UTC — dort also ein Zeitfenster.
    const day = berlinDay("2026-10-07");
    expect(eta(day, { ...de, timeZone: "UTC" })?.kind).toBe("window");
    // Und umgekehrt: Mitternacht UTC ist in Berlin 2 Uhr nachts.
    const utcDay = { eta_start: "2026-10-07T00:00:00Z", eta_end: "2026-10-08T00:00:00Z" };
    expect(eta(utcDay)?.kind).toBe("window");
    expect(eta(utcDay, { ...de, timeZone: "UTC" })).toEqual({ kind: "allday", text: "morgen" });
  });

  it("Uhrzeiten in der HA-Zeitzone", () => {
    const window = { eta_start: "2026-10-06T12:00:00Z", eta_end: "2026-10-06T16:00:00Z" };
    expect(eta(window, { ...de, timeZone: "America/New_York" })?.text).toBe("heute 08–12 Uhr");
  });

  it("zugestellt: Zeitpunkt der Zustellung statt Termin", () => {
    const label = eta({
      status: "delivered",
      delivered_at: "2026-10-06T07:15:00Z",
      eta_start: "2026-10-06T12:00:00Z",
    });
    expect(label?.kind).toBe("delivered");
    expect(label?.text).toMatch(/^heute 0?9:15/);
  });

  it("ohne Termin nichts", () => {
    expect(eta({})).toBeUndefined();
    expect(eta({ eta_start: "kein Datum" })).toBeUndefined();
  });
});

describe("Zeitstempel", () => {
  it("Tag und Uhrzeit", () => {
    expect(normalize(formatTimestamp("2026-10-06T07:00:00Z", de))).toBe("heute 9:00");
    expect(formatTimestamp(undefined, de)).toBeUndefined();
  });
});
