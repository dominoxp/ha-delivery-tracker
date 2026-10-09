import { afterEach, describe, expect, it } from "vitest";

import { validateConfig } from "../src/config";
import "../src/editor";
import type { DeliveryTrackerCardEditor } from "../src/editor";
import { ENTITY, entity, hass, shipmentSensor } from "./fixtures";

type Config = Record<string, unknown>;

async function mount(
  config: Config,
): Promise<{ editor: DeliveryTrackerCardEditor; events: Config[] }> {
  const editor = document.createElement("delivery-tracker-card-editor");
  editor.hass = hass(shipmentSensor(), entity("sensor.temperatur", {}));
  editor.setConfig(config);
  const events: Config[] = [];
  editor.addEventListener("config-changed", (ev) =>
    events.push((ev as CustomEvent<{ config: Config }>).detail.config),
  );
  document.body.append(editor);
  await editor.updateComplete;
  return { editor, events };
}

function root(editor: DeliveryTrackerCardEditor): ShadowRoot {
  return editor.shadowRoot as ShadowRoot;
}

async function check(editor: DeliveryTrackerCardEditor, group: string, value: string) {
  const box = root(editor).querySelector<HTMLInputElement>(
    `fieldset[name="${group}"] input[value="${value}"]`,
  );
  if (!box) {
    throw new Error(`Kein Kontrollkaestchen ${group}/${value}`);
  }
  box.click();
  await editor.updateComplete;
}

async function choose(editor: DeliveryTrackerCardEditor, name: string, value: string) {
  const element = root(editor).querySelector<HTMLSelectElement | HTMLInputElement>(
    `[name="${name}"]`,
  );
  if (!element) {
    throw new Error(`Kein Feld ${name}`);
  }
  element.value = value;
  const type = element instanceof HTMLInputElement && element.type === "text" ? "input" : "change";
  element.dispatchEvent(new Event(type));
  await editor.updateComplete;
}

function options(editor: DeliveryTrackerCardEditor, group: string): string[] {
  return [
    ...root(editor).querySelectorAll<HTMLInputElement>(`fieldset[name="${group}"] input`),
  ].map((input) => input.value);
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("Editor", () => {
  it("bietet nur Sensoren mit Sendungsliste an", async () => {
    const { editor } = await mount({ type: "custom:delivery-tracker-card", entity: "" });
    const values = [...root(editor).querySelectorAll("select[name=entity] option")].map((option) =>
      option.getAttribute("value"),
    );
    expect(values).toEqual(["", ENTITY]);
  });

  it("bietet Zielorte und Zusteller aus dem Sensor an", async () => {
    const { editor } = await mount({ type: "custom:delivery-tracker-card", entity: ENTITY });
    expect(options(editor, "destinations")).toEqual(["home", "buero", "unknown"]);
    expect(options(editor, "carriers")).toEqual(["amazon", "dhl", "dpd", "hermes"]);
    expect(options(editor, "statuses")).toHaveLength(11);
    expect(options(editor, "fields")).toHaveLength(9);
  });

  it("jede Option ist bedienbar und ergibt dieselbe Konfiguration wie von Hand", async () => {
    const { editor, events } = await mount({ type: "custom:delivery-tracker-card", entity: "" });

    await choose(editor, "entity", ENTITY);
    await choose(editor, "title", "Pakete");
    await check(editor, "destinations", "home");
    await check(editor, "destinations", "unknown");
    await check(editor, "statuses", "in_transit");
    await check(editor, "statuses", "delayed");
    await check(editor, "carriers", "dhl");
    await choose(editor, "group_by", "destination");
    await choose(editor, "sort_by", "last_updated");
    await choose(editor, "max_items", "5");
    await check(editor, "fields", "tracking_number");
    await check(editor, "fields", "progress");

    const handWritten = {
      type: "custom:delivery-tracker-card",
      entity: ENTITY,
      title: "Pakete",
      destinations: ["home", "unknown"],
      // Von Hand in anderer Reihenfolge — die Normalform ist dieselbe.
      statuses: ["delayed", "in_transit"],
      carriers: ["dhl"],
      group_by: "destination",
      sort_by: "last_updated",
      max_items: 5,
      fields: ["carrier", "status", "eta", "sender", "description", "destination", "pickup"],
    };
    const last = events.at(-1);
    expect(last).toEqual(validateConfig(handWritten));
    expect(JSON.stringify(last)).toBe(JSON.stringify(validateConfig(handWritten)));
  });

  it("Standardwerte werden nicht geschrieben", async () => {
    const { editor, events } = await mount({
      type: "custom:delivery-tracker-card",
      entity: ENTITY,
      statuses: ["delayed"],
      group_by: "status",
      max_items: 3,
      fields: ["status"],
    });

    await check(editor, "statuses", "delayed");
    await choose(editor, "group_by", "none");
    await choose(editor, "max_items", "");
    for (const field of ["carrier", "progress", "eta", "sender", "description"]) {
      await check(editor, "fields", field);
    }
    for (const field of ["destination", "pickup", "tracking_number"]) {
      await check(editor, "fields", field);
    }
    expect(events.at(-1)).toEqual({ type: "custom:delivery-tracker-card", entity: ENTITY });
  });

  it("laesst fremde Schluessel von HA unangetastet", async () => {
    const { editor, events } = await mount({
      type: "custom:delivery-tracker-card",
      entity: ENTITY,
      grid_options: { columns: 6 },
      visibility: [{ condition: "screen" }],
    });
    await choose(editor, "sort_by", "status");
    expect(events.at(-1)).toEqual({
      type: "custom:delivery-tracker-card",
      entity: ENTITY,
      sort_by: "status",
      grid_options: { columns: 6 },
      visibility: [{ condition: "screen" }],
    });
  });

  it("das letzte Feld laesst sich nicht abwaehlen", async () => {
    const { editor } = await mount({
      type: "custom:delivery-tracker-card",
      entity: ENTITY,
      fields: ["eta"],
    });
    const box = root(editor).querySelector<HTMLInputElement>(
      'fieldset[name="fields"] input[value="eta"]',
    );
    expect(box?.disabled).toBe(true);
  });
});
