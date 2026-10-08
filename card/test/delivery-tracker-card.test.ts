import { afterEach, describe, expect, it, vi } from "vitest";

import { DeliveryTrackerCard } from "../src/delivery-tracker-card";
import type { HassEntity, HomeAssistant } from "../src/types";

const ENTITY = "sensor.delivery_tracker_aktive_sendungen";

function entity(entityId: string, attributes: Record<string, unknown>, state = "0"): HassEntity {
  return {
    entity_id: entityId,
    state,
    attributes,
    last_changed: "2026-10-06T08:00:00+00:00",
    last_updated: "2026-10-06T08:00:00+00:00",
  };
}

function hass(...entities: HassEntity[]): HomeAssistant {
  return {
    states: Object.fromEntries(entities.map((e) => [e.entity_id, e])),
    locale: { language: "de" },
  };
}

async function mount(config: unknown, hassObj: HomeAssistant): Promise<DeliveryTrackerCard> {
  const card = document.createElement("delivery-tracker-card");
  card.hass = hassObj;
  card.setConfig(config);
  document.body.append(card);
  await card.updateComplete;
  return card;
}

function text(card: DeliveryTrackerCard): string {
  return card.shadowRoot?.textContent?.replace(/\s+/g, " ").trim() ?? "";
}

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("Registrierung", () => {
  it("definiert das Custom Element", () => {
    expect(customElements.get("delivery-tracker-card")).toBe(DeliveryTrackerCard);
  });

  it("traegt die Karte genau einmal in die Kartenauswahl ein", () => {
    const entries = window.customCards?.filter((c) => c.type === "delivery-tracker-card");
    expect(entries).toHaveLength(1);
    expect(entries?.[0]).toMatchObject({ name: "Delivery Tracker", preview: true });
  });
});

describe("Rendern", () => {
  it("zeigt Titel und Anzahl der Sendungen", async () => {
    const card = await mount(
      { type: "custom:delivery-tracker-card", entity: ENTITY, title: "Pakete" },
      hass(entity(ENTITY, { shipments: [{}, {}] }, "2")),
    );
    const haCard = card.shadowRoot?.querySelector("ha-card") as
      (HTMLElement & { header?: string }) | null;
    expect(haCard?.header).toBe("Pakete");
    expect(text(card)).toContain("2 Sendung(en)");
  });

  it("nimmt ohne Titel den Namen der Entity", async () => {
    const card = await mount(
      { entity: ENTITY },
      hass(entity(ENTITY, { shipments: [], friendly_name: "Aktive Sendungen" })),
    );
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

  it("rendert bei fremden Zustandsaenderungen nicht neu", async () => {
    const own = entity(ENTITY, { shipments: [] });
    const card = await mount({ entity: ENTITY }, hass(own));
    const render = vi.spyOn(card as unknown as { render: () => unknown }, "render");

    card.hass = hass(own, entity("sensor.temperatur", {}, "21"));
    await card.updateComplete;
    expect(render).not.toHaveBeenCalled();

    card.hass = hass(entity(ENTITY, { shipments: [{}] }, "1"));
    await card.updateComplete;
    expect(render).toHaveBeenCalledOnce();
    expect(text(card)).toContain("1 Sendung(en)");
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
  ])("lehnt %j mit lesbarer Meldung ab", (config, message) => {
    card.hass = hass();
    expect(() => card.setConfig(config)).toThrow(message);
  });

  it("schlaegt in der Kartenauswahl einen Sensor mit Sendungsliste vor", () => {
    const stub = DeliveryTrackerCard.getStubConfig(
      hass(entity("sensor.temperatur", {}), entity(ENTITY, { shipments: [] })),
    );
    expect(stub).toEqual({ type: "custom:delivery-tracker-card", entity: ENTITY });
  });
});
