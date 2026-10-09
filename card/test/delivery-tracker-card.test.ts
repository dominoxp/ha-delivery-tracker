import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeliveryTrackerCard } from "../src/delivery-tracker-card";
import type { HomeAssistant } from "../src/types";
import { ENTITY, NOW, entity, hass, normalize, sampleShipments, shipmentSensor } from "./fixtures";

async function mount(config: unknown, hassObj: HomeAssistant): Promise<DeliveryTrackerCard> {
  const card = document.createElement("delivery-tracker-card");
  card.hass = hassObj;
  card.setConfig(config);
  document.body.append(card);
  await card.updateComplete;
  return card;
}

function text(card: DeliveryTrackerCard | Element | null | undefined): string {
  const root = card instanceof DeliveryTrackerCard ? card.shadowRoot : card;
  return normalize(root?.textContent);
}

function rows(card: DeliveryTrackerCard): HTMLElement[] {
  return [...(card.shadowRoot?.querySelectorAll<HTMLElement>(".shipment") ?? [])];
}

function row(card: DeliveryTrackerCard, name: string): HTMLElement {
  const found = rows(card).find((item) => text(item).includes(name));
  if (!found) {
    throw new Error(`Keine Zeile mit ${name}`);
  }
  return found;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("Registrierung", () => {
  it("definiert Karte und Editor", () => {
    expect(customElements.get("delivery-tracker-card")).toBe(DeliveryTrackerCard);
    expect(DeliveryTrackerCard.getConfigElement().tagName.toLowerCase()).toBe(
      "delivery-tracker-card-editor",
    );
  });

  it("traegt die Karte genau einmal in die Kartenauswahl ein", () => {
    const entries = window.customCards?.filter((c) => c.type === "delivery-tracker-card");
    expect(entries).toHaveLength(1);
    expect(entries?.[0]).toMatchObject({ name: "Delivery Tracker", preview: true });
  });
});

describe("Sendungsliste", () => {
  it("zeigt alle Sendungen mit allen Feldern", async () => {
    const card = await mount({ entity: ENTITY, title: "Pakete" }, hass(shipmentSensor()));
    const haCard = card.shadowRoot?.querySelector("ha-card") as HTMLElement & { header?: string };
    expect(haCard.header).toBe("Pakete");
    expect(rows(card)).toHaveLength(4);

    const dhl = text(row(card, "Fahrradladen"));
    for (const expected of [
      "Fahrradladen",
      "Fahrradschlauch",
      "DHL Paket",
      "In Zustellung",
      "→ Zuhause",
      "00340434161094042557",
      "heute 14–18 Uhr",
    ]) {
      expect(dhl).toContain(expected);
    }
    const locker = text(row(card, "Amazon Locker"));
    expect(locker).toContain("Amazon Locker Hauptbahnhof");
    expect(locker).toContain("Abholcode: 482913");
    expect(locker).toContain("→ Unbekannter Zielort");
  });

  it("unterscheidet ganztaegige Termine und Zeitfenster sichtbar", async () => {
    const card = await mount({ entity: ENTITY }, hass(shipmentSensor()));
    const window = row(card, "Fahrradladen").querySelector(".eta");
    const allDay = row(card, "Werkzeughandel").querySelector(".eta");
    expect(window?.classList.contains("eta-window")).toBe(true);
    expect(allDay?.classList.contains("eta-allday")).toBe(true);
    expect(text(allDay)).toBe("morgen");
    expect(allDay?.getAttribute("title")).toBe("Ganztägig");
  });

  it("zeigt nur die gewaehlten Felder", async () => {
    const card = await mount({ entity: ENTITY, fields: ["status", "eta"] }, hass(shipmentSensor()));
    const dhl = row(card, "In Zustellung");
    expect(text(dhl)).toContain("heute 14–18 Uhr");
    // Ohne Absender und Beschreibung wird der Zusteller zur Ueberschrift.
    expect(text(dhl.querySelector(".name"))).toBe("DHL Paket");
    expect(text(dhl)).not.toContain("Fahrradladen");
    expect(dhl.querySelector(".badge, .progress, .destination")).toBeNull();
    expect(text(row(card, "Abholbereit"))).not.toContain("482913");
  });

  it("filtert und gruppiert nach Zielort mit beschrifteten Abschnitten", async () => {
    const card = await mount(
      { entity: ENTITY, group_by: "destination", statuses: ["out_for_delivery", "delayed"] },
      hass(shipmentSensor()),
    );
    const groups = [...(card.shadowRoot?.querySelectorAll("section.group") ?? [])];
    expect(
      groups.map((group) => [
        text(group.querySelector(".group-title > span")),
        text(group.querySelector(".group-count")),
      ]),
    ).toEqual([
      ["Zuhause", "1"],
      ["Büro", "1"],
    ]);
    expect(text(groups[0])).toContain("Fahrradladen");
    expect(text(groups[1])).toContain("Werkzeughandel");
  });

  it("begrenzt die Anzahl und nennt den Rest", async () => {
    const card = await mount({ entity: ENTITY, max_items: 2 }, hass(shipmentSensor()));
    expect(rows(card)).toHaveLength(2);
    expect(text(card.shadowRoot?.querySelector(".more"))).toBe("+2 weitere");
  });
});

describe("Fortschritt", () => {
  it("normaler Verlauf als Stufen", async () => {
    const card = await mount({ entity: ENTITY }, hass(shipmentSensor()));
    const bar = row(card, "Fahrradladen").querySelector(".progress");
    expect(bar?.getAttribute("role")).toBe("progressbar");
    expect(bar?.getAttribute("aria-valuenow")).toBe("4");
    expect(bar?.querySelectorAll(".segment.filled")).toHaveLength(4);
  });

  it("Verzoegerung bleibt Fortschritt, aber als Warnung", async () => {
    const card = await mount({ entity: ENTITY }, hass(shipmentSensor()));
    const dpd = row(card, "Werkzeughandel");
    expect(dpd.classList.contains("tone-warning")).toBe(true);
    expect(dpd.querySelector(".progress")?.getAttribute("aria-valuenow")).toBe("3");
  });

  it("Retour und gescheiterte Zustellung sind kein Fortschritt", async () => {
    const shipments = [
      ...sampleShipments(),
      { id: "gescheitert", carrier: "dhl", status: "delivery_failed", sender: "Baumarkt" },
    ];
    const card = await mount({ entity: ENTITY }, hass(shipmentSensor(shipments)));
    for (const [name, status] of [
      ["Modeversand", "Zurück an Absender"],
      ["Baumarkt", "Zustellung gescheitert"],
    ]) {
      const item = row(card, name);
      const bar = item.querySelector(".progress");
      expect(item.classList.contains("tone-exception")).toBe(true);
      expect(bar?.classList.contains("exception")).toBe(true);
      expect(bar?.getAttribute("role")).toBe("img");
      expect(bar?.getAttribute("aria-valuenow")).toBeNull();
      expect(bar?.getAttribute("aria-label")).toBe(`Nicht zugestellt: ${status}`);
    }
  });
});

describe("Details", () => {
  it("Klick oeffnet die Details mit Link zur Sendungsverfolgung", async () => {
    const card = await mount({ entity: ENTITY }, hass(shipmentSensor()));
    const dhl = row(card, "Fahrradladen");
    const button = dhl.querySelector<HTMLButtonElement>("button.row");
    expect(button?.getAttribute("aria-expanded")).toBe("false");
    expect(dhl.querySelector(".details")).toBeNull();

    button?.click();
    await card.updateComplete;

    expect(button?.getAttribute("aria-expanded")).toBe("true");
    const details = row(card, "Fahrradladen").querySelector(".details");
    expect(text(details)).toContain("Die Sendung wurde in das Zustellfahrzeug geladen.");
    const link = details?.querySelector("a");
    expect(link?.getAttribute("href")).toContain("https://www.dhl.de/");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");

    button?.click();
    await card.updateComplete;
    expect(row(card, "Fahrradladen").querySelector(".details")).toBeNull();
  });

  it("bietet nur http(s)-Links an", async () => {
    const card = await mount({ entity: ENTITY }, hass(shipmentSensor()));
    row(card, "Werkzeughandel").querySelector<HTMLButtonElement>("button.row")?.click();
    await card.updateComplete;
    const details = row(card, "Werkzeughandel").querySelector(".details");
    expect(details).not.toBeNull();
    expect(details?.querySelector("a")).toBeNull();
  });
});

describe("Keine externen Ressourcen", () => {
  it("Symbole sind eingebettet, nichts wird nachgeladen", async () => {
    const card = await mount({ entity: ENTITY }, hass(shipmentSensor()));
    const root = card.shadowRoot as ShadowRoot;
    expect(root.querySelectorAll(".badge svg")).toHaveLength(4);
    expect(root.querySelector("img, image, use, link, [src]")).toBeNull();
  });
});

describe("Leer- und Fehlerzustand", () => {
  it("keine Sendungen", async () => {
    const card = await mount({ entity: ENTITY }, hass(shipmentSensor([])));
    expect(card.shadowRoot?.querySelector(".empty svg")).not.toBeNull();
    expect(text(card.shadowRoot?.querySelector(".empty"))).toBe("Keine Sendungen");
  });

  it("nichts passt zum Filter", async () => {
    const card = await mount({ entity: ENTITY, carriers: ["ups"] }, hass(shipmentSensor()));
    expect(text(card.shadowRoot?.querySelector(".empty"))).toBe(
      "Keine Sendungen passen zum Filter",
    );
  });

  it("nimmt ohne Titel den Namen der Entity", async () => {
    const card = await mount({ entity: ENTITY }, hass(shipmentSensor([])));
    const haCard = card.shadowRoot?.querySelector("ha-card") as HTMLElement & { header?: string };
    expect(haCard.header).toBe("Aktive Sendungen");
  });

  it("meldet eine fehlende Entity", async () => {
    const card = await mount({ entity: ENTITY }, hass());
    expect(card.shadowRoot?.querySelector(".warning")).not.toBeNull();
    expect(text(card)).toContain("nicht gefunden");
  });

  it("meldet eine nicht verfuegbare Entity", async () => {
    const card = await mount({ entity: ENTITY }, hass(entity(ENTITY, {}, "unavailable")));
    expect(text(card)).toContain("nicht verfügbar");
  });

  it("meldet einen Sensor ohne Sendungsliste", async () => {
    const card = await mount(
      { entity: "sensor.temperatur" },
      hass(entity("sensor.temperatur", {})),
    );
    expect(text(card)).toContain("kein Sensor der Delivery-Tracker-Integration");
  });
});

describe("Aktualisierung", () => {
  it("rendert bei fremden Zustandsaenderungen nicht neu", async () => {
    const own = shipmentSensor([]);
    const card = await mount({ entity: ENTITY }, hass(own));
    const render = vi.spyOn(card as unknown as { render: () => unknown }, "render");

    card.hass = hass(own, entity("sensor.temperatur", {}, "21"));
    await card.updateComplete;
    expect(render).not.toHaveBeenCalled();

    card.hass = hass(shipmentSensor());
    await card.updateComplete;
    expect(render).toHaveBeenCalledOnce();
    expect(rows(card)).toHaveLength(4);
  });
});

describe("Konfiguration", () => {
  const card = new DeliveryTrackerCard();

  it.each([
    [null, "muss ein Objekt sein"],
    [{}, "Bitte unter `entity`"],
    [{ entity: "light.flur" }, "Sensor-Entity-ID"],
    [{ entity: 42 }, "Sensor-Entity-ID"],
    [{ entity: ENTITY, title: 1 }, "`title` muss ein Text sein"],
    [{ entity: ENTITY, statuses: ["verloren"] }, "unbekannten Wert `verloren`"],
    [{ entity: ENTITY, fields: ["farbe"] }, "unbekannten Wert `farbe`"],
    [{ entity: ENTITY, destinations: "home" }, "`destinations` muss eine Liste"],
    [{ entity: ENTITY, carriers: [1] }, "`carriers` muss eine Liste"],
    [{ entity: ENTITY, group_by: "farbe" }, "`group_by` muss einer der Werte"],
    [{ entity: ENTITY, sort_by: "zufall" }, "`sort_by` muss einer der Werte"],
    [{ entity: ENTITY, max_items: 0 }, "`max_items` muss eine ganze Zahl"],
    [{ entity: ENTITY, max_items: "5" }, "`max_items` muss eine ganze Zahl"],
  ])("lehnt %j mit lesbarer Meldung ab", (config, message) => {
    card.hass = hass();
    expect(() => card.setConfig(config)).toThrow(message);
  });

  it("schlaegt in der Kartenauswahl einen Sensor mit Sendungsliste vor", () => {
    const stub = DeliveryTrackerCard.getStubConfig(
      hass(entity("sensor.temperatur", {}), shipmentSensor([])),
    );
    expect(stub).toEqual({ type: "custom:delivery-tracker-card", entity: ENTITY });
  });
});
