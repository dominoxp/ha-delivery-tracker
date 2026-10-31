/**
 * Entwicklungsvorschau der Karte (`npm run dev`): bindet die Quellen direkt
 * ein, Vite laedt bei jeder Aenderung neu. Gehoert nicht zum Build.
 *
 * `ha-card` und die Theme-Variablen liefert in HA das Frontend; hier sind sie
 * nachgebildet.
 */
import "../src/delivery-tracker-card";
import type { DeliveryTrackerCard } from "../src/delivery-tracker-card";
import type { DeliveryTrackerCardEditor } from "../src/editor";
import type { HassEntity, HomeAssistant } from "../src/types";

class HaCardStub extends HTMLElement {
  private readonly title_: HTMLDivElement;

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `<style>
      :host { display: block; background: var(--ha-card-background); color: var(--primary-text-color);
        border: 1px solid var(--divider-color); border-radius: 12px; }
      .header { padding: 16px 16px 8px; font-size: 1.25rem; }
      .header:empty { display: none; }
      ::slotted(.card-content) { padding: 0 16px 16px; }
    </style><div class="header"></div><slot></slot>`;
    this.title_ = root.querySelector(".header") as HTMLDivElement;
  }

  set header(value: string | undefined) {
    this.title_.textContent = value ?? "";
  }
}
if (!customElements.get("ha-card")) {
  customElements.define("ha-card", HaCardStub);
}

// ----------------------------------------------------------------------
// Beispieldaten im Format von `Shipment.as_summary()`, relativ zu heute
// ----------------------------------------------------------------------

const TZ = "Europe/Berlin";
const [year, month, day] = new Intl.DateTimeFormat("en-CA", {
  timeZone: TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
})
  .format(new Date())
  .split("-")
  .map(Number);

/** UTC-Abstand der Zeitzone an einem Tag, z.B. „+02:00". */
function offsetFor(ymd: string): string {
  const name =
    new Intl.DateTimeFormat("en-US", { timeZone: TZ, timeZoneName: "longOffset" })
      .formatToParts(new Date(`${ymd}T12:00:00Z`))
      .find((part) => part.type === "timeZoneName")?.value ?? "GMT";
  return name === "GMT" ? "+00:00" : name.slice(3);
}

/** Zeitpunkt in Berlin, `days` Tage nach heute. */
function at(days: number, hour = 0, minute = 0): string {
  const ymd = new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
  const time = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
  return new Date(`${ymd}T${time}:00${offsetFor(ymd)}`).toISOString();
}

const ago = (hours: number): string => new Date(Date.now() - hours * 3_600_000).toISOString();

const shipments = [
  {
    id: "dhl_heute",
    carrier: "dhl",
    tracking_number: "00340434161094042557",
    status: "out_for_delivery",
    status_text: "Die Sendung wurde in das Zustellfahrzeug geladen.",
    eta_start: at(0, 14),
    eta_end: at(0, 18),
    destination_id: "home",
    sender: "Fahrradladen Kettenblatt",
    description: "Fahrradschlauch 28 Zoll (+1 weiterer Artikel)",
    tracking_url:
      "https://www.dhl.de/de/privatkunden/dhl-sendungsverfolgung.html?piececode=00340434161094042557",
    last_updated: ago(2),
  },
  {
    id: "dpd_morgen",
    carrier: "dpd",
    tracking_number: "09981122334455",
    status: "delayed",
    status_text: "Die Zustellung verzögert sich um einen Tag.",
    eta_start: at(1),
    eta_end: at(2),
    destination_id: "buero",
    sender: "Werkzeughandel Becker",
    description: "Akkuschrauber 18 V",
    tracking_url: "https://tracking.dpd.de/status/de_DE/parcel/09981122334455",
    last_updated: ago(14),
  },
  {
    id: "gls_spaeter",
    carrier: "gls",
    tracking_number: "50123456789",
    status: "in_transit",
    eta_start: at(3),
    eta_end: at(4),
    destination_id: "home",
    sender: "Gartencenter Grünfink",
    description: "Saatgut-Set Wintergemüse",
    last_updated: ago(20),
  },
  {
    id: "amazon_locker",
    carrier: "amazon",
    status: "ready_for_pickup",
    status_text: "Ihr Paket kann abgeholt werden.",
    destination_id: "unknown",
    sender: "Amazon",
    description: "Kochbuch (+2 weitere Artikel)",
    pickup_location: "Amazon Locker Hauptbahnhof, Bahnhofplatz 1",
    pickup_code: "482913",
    last_updated: ago(1),
  },
  {
    id: "ups_zoll",
    carrier: "ups",
    tracking_number: "1Z0E12345678901235",
    status: "customs",
    status_text: "Import scan",
    destination_id: "home",
    sender: "Camera Shop Ltd.",
    description: "Objektiv 35 mm",
    last_updated: ago(30),
  },
  {
    id: "post_zugestellt",
    carrier: "dhl",
    tracking_number: "RR123456785DE",
    status: "delivered",
    delivered_at: at(0, 9, 12),
    destination_id: "home",
    sender: "Buchhandlung am Markt",
    description: "Roman",
    last_updated: ago(3),
  },
  {
    id: "dhl_gescheitert",
    carrier: "dhl",
    tracking_number: "00340434161094099999",
    status: "delivery_failed",
    status_text: "Empfänger nicht angetroffen, Sendung geht zurück.",
    destination_id: "buero",
    sender: "Druckerei Satzspiegel",
    description: "Visitenkarten",
    last_updated: ago(6),
  },
  {
    id: "hermes_retour",
    carrier: "hermes",
    tracking_number: "H1001234567890123451",
    status: "returned",
    destination_id: "home",
    sender: "Modeversand Nordlicht",
    description: "Regenjacke",
    last_updated: ago(50),
  },
];

const names = {
  carriers: {
    dhl: "DHL Paket",
    dpd: "DPD",
    gls: "GLS",
    amazon: "Amazon",
    ups: "UPS",
    hermes: "Hermes",
  },
  destinations: { home: "Zuhause", buero: "Büro" },
};

function entity(entityId: string, state: string, attributes: Record<string, unknown>): HassEntity {
  return { entity_id: entityId, state, attributes, last_changed: ago(1), last_updated: ago(1) };
}

const hass: HomeAssistant = {
  locale: { language: "de" },
  config: { time_zone: TZ },
  callService: (domain, service, data) => {
    console.info("callService", domain, service, data);
    return Promise.resolve();
  },
  states: Object.fromEntries(
    [
      entity("sensor.delivery_tracker_aktive_sendungen", String(shipments.length), {
        friendly_name: "Aktive Sendungen",
        shipments,
        ...names,
      }),
      entity("sensor.delivery_tracker_buero", "0", {
        friendly_name: "Büro",
        shipments: [],
        ...names,
      }),
      entity("sensor.delivery_tracker_zuhause", "unavailable", { friendly_name: "Zuhause" }),
      entity("sensor.paket_alt", "3", { friendly_name: "Paket (alt)" }),
    ].map((state) => [state.entity_id, state]),
  ),
};

// ----------------------------------------------------------------------
// Karten, Editor und Theme-Umschalter
// ----------------------------------------------------------------------

for (const card of document.querySelectorAll<DeliveryTrackerCard>("delivery-tracker-card")) {
  card.hass = hass;
  card.setConfig(JSON.parse(card.dataset.config ?? "{}"));
  if (card.dataset.open) {
    card._expanded = new Set([card.dataset.open]);
  }
}

const editor = document.getElementById("editor") as DeliveryTrackerCardEditor;
const preview = document.getElementById("editor-preview") as DeliveryTrackerCard;
const yaml = document.getElementById("editor-yaml") as HTMLPreElement;

const toYaml = (config: Record<string, unknown>): string =>
  Object.entries(config)
    .map(([key, value]) => `${key}: ${Array.isArray(value) ? `[${value.join(", ")}]` : value}`)
    .join("\n");

const initial = {
  type: "custom:delivery-tracker-card",
  ...JSON.parse(preview.dataset.config ?? "{}"),
};
editor.hass = hass;
editor.setConfig(initial);
yaml.textContent = toYaml(initial);
editor.addEventListener("config-changed", (ev) => {
  const { config } = (ev as CustomEvent<{ config: Record<string, unknown> }>).detail;
  yaml.textContent = toYaml(config);
  try {
    preview.setConfig(config);
  } catch (err) {
    yaml.textContent += `\n# ${(err as Error).message}`;
  }
});

for (const button of document.querySelectorAll<HTMLButtonElement>("[data-theme-choice]")) {
  button.addEventListener("click", () => {
    const choice = button.dataset.themeChoice;
    if (choice) {
      document.documentElement.dataset.theme = choice;
    } else {
      delete document.documentElement.dataset.theme;
    }
    for (const other of document.querySelectorAll("[data-theme-choice]")) {
      other.setAttribute("aria-pressed", String(other === button));
    }
  });
}
