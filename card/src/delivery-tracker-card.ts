import { LitElement, css, html, nothing, type PropertyValues, type TemplateResult } from "lit";

import { CARD_TYPE, validateConfig, type DeliveryTrackerCardConfig } from "./config";
import { localize, type StringKey } from "./localize";
import type { HassEntity, HomeAssistant } from "./types";

/**
 * Grundgeruest der Delivery-Tracker-Karte.
 *
 * Nutzt vom HA-Frontend nur `ha-card` und die Theme-Variablen; Farben kommen
 * ausschliesslich aus dem Theme, damit helles und dunkles Theme passen.
 */
export class DeliveryTrackerCard extends LitElement {
  static override properties = {
    hass: { attribute: false },
    _config: { state: true },
  };

  declare hass?: HomeAssistant;
  declare _config?: DeliveryTrackerCardConfig;

  /** Vorschlag fuer die Kartenauswahl: der erste Sensor mit Sendungsliste. */
  static getStubConfig(hass: HomeAssistant): DeliveryTrackerCardConfig {
    const entity = Object.values(hass.states).find(
      (state) =>
        state?.entity_id.startsWith("sensor.") && Array.isArray(state.attributes.shipments),
    );
    return { type: `custom:${CARD_TYPE}`, entity: entity?.entity_id ?? "" };
  }

  setConfig(config: unknown): void {
    this._config = validateConfig(config, this.language);
  }

  getCardSize(): number {
    return 2;
  }

  getGridOptions(): { columns: number; min_rows: number } {
    return { columns: 12, min_rows: 2 };
  }

  /**
   * `hass` wird bei jeder Zustandsaenderung im ganzen System neu gesetzt. Neu
   * gerendert wird nur, wenn sich die eigene Entity oder die Sprache aendert.
   */
  protected override shouldUpdate(changed: PropertyValues<this>): boolean {
    if (!this._config) {
      return false;
    }
    if (changed.has("_config")) {
      return true;
    }
    const oldHass = changed.get("hass") as HomeAssistant | undefined;
    if (!oldHass || !this.hass) {
      return changed.has("hass");
    }
    const { entity } = this._config;
    return (
      oldHass.states[entity] !== this.hass.states[entity] ||
      oldHass.locale.language !== this.hass.locale.language
    );
  }

  protected override render(): TemplateResult | typeof nothing {
    if (!this._config || !this.hass) {
      return nothing;
    }
    const { entity } = this._config;
    const stateObj = this.hass.states[entity];
    const title =
      this._config.title ??
      stateObj?.attributes.friendly_name ??
      localize(this.language, "card.default_title");

    return html`<ha-card .header=${title}>
      <div class="card-content">${this.renderBody(entity, stateObj)}</div>
    </ha-card>`;
  }

  private renderBody(entity: string, stateObj: HassEntity | undefined): TemplateResult {
    if (!stateObj) {
      return this.renderWarning("error.entity_not_found", entity);
    }
    if (stateObj.state === "unavailable") {
      return this.renderWarning("error.entity_unavailable", entity);
    }
    const shipments = stateObj.attributes.shipments;
    if (!Array.isArray(shipments)) {
      return this.renderWarning("error.not_a_shipment_sensor", entity);
    }
    // Die Sendungsliste folgt in 4.2.
    return html`<div class="summary">
      ${localize(this.language, "card.count", { count: shipments.length })}
    </div>`;
  }

  private renderWarning(key: StringKey, entity: string): TemplateResult {
    return html`<div class="warning" role="alert">
      ${localize(this.language, key, { entity })}
    </div>`;
  }

  private get language(): string | undefined {
    return this.hass?.locale.language ?? globalThis.navigator?.language;
  }

  static override styles = css`
    .summary {
      color: var(--secondary-text-color);
    }
    .warning {
      padding: 8px 12px;
      border-radius: var(--ha-card-border-radius, 12px);
      border-left: 4px solid var(--warning-color);
      background: color-mix(in srgb, var(--warning-color) 12%, transparent);
      color: var(--primary-text-color);
      overflow-wrap: anywhere;
    }
  `;
}

// Die Datei kann doppelt geladen werden (von der Integration und zusaetzlich als
// manuell eingetragene Ressource) — ein zweites `define` wuerde werfen.
if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, DeliveryTrackerCard);
}

window.customCards = window.customCards ?? [];
if (!window.customCards.some((card) => card.type === CARD_TYPE)) {
  window.customCards.push({
    type: CARD_TYPE,
    name: localize(globalThis.navigator?.language, "card.name"),
    description: localize(globalThis.navigator?.language, "card.description"),
    preview: true,
    documentationURL: "https://github.com/dominoxp/ha-delivery-tracker#lovelace-karte",
  });
}

declare global {
  interface HTMLElementTagNameMap {
    "delivery-tracker-card": DeliveryTrackerCard;
  }
}
