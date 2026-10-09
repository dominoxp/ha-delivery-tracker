import { LitElement, css, html, nothing, type PropertyValues, type TemplateResult } from "lit";

import {
  CARD_TYPE,
  effectiveFields,
  validateConfig,
  type DeliveryTrackerCardConfig,
  type Field,
} from "./config";
import "./editor";
import { formatEta, formatTimestamp, type FormatContext } from "./format";
import {
  carrierIcon,
  iconCalendar,
  iconCheck,
  iconChevron,
  iconClock,
  iconEmpty,
  iconExternal,
} from "./icons";
import { makeLabels } from "./labels";
import { buildView, type Labels, type ShipmentGroup } from "./list";
import { localize, type StringKey } from "./localize";
import {
  PROGRESS_STEPS,
  destinationOf,
  parseShipments,
  progressOf,
  safeTrackingUrl,
  type Shipment,
  type ShipmentStatus,
} from "./shipment";
import type { HassEntity, HomeAssistant } from "./types";

/** Alle Werte, die eine Zeile zum Rendern braucht. */
interface RowContext {
  fields: ReadonlySet<Field>;
  labels: Labels;
  format: FormatContext;
}

/**
 * Die Delivery-Tracker-Karte: Sendungsliste aus dem Attribut `shipments`.
 *
 * Nutzt vom HA-Frontend nur `ha-card` und die Theme-Variablen; Farben kommen
 * ausschliesslich aus dem Theme, damit helles und dunkles Theme passen.
 * Die Karte liest nur das Attribut und ruft keine Action auf.
 */
export class DeliveryTrackerCard extends LitElement {
  static override properties = {
    hass: { attribute: false },
    _config: { state: true },
    _expanded: { state: true },
    _tick: { state: true },
  };

  declare hass?: HomeAssistant;
  declare _config?: DeliveryTrackerCardConfig;
  /** IDs der aufgeklappten Sendungen. */
  declare _expanded: ReadonlySet<string>;
  /** Minutentakt: „heute" wird um Mitternacht zu „gestern". */
  declare _tick: number;

  private _timer?: ReturnType<typeof setInterval>;

  constructor() {
    super();
    this._expanded = new Set();
    this._tick = 0;
  }

  /** Vorschlag fuer die Kartenauswahl: der erste Sensor mit Sendungsliste. */
  static getStubConfig(hass: HomeAssistant): DeliveryTrackerCardConfig {
    const entity = Object.values(hass.states).find(
      (state) =>
        state?.entity_id.startsWith("sensor.") && Array.isArray(state.attributes.shipments),
    );
    return { type: `custom:${CARD_TYPE}`, entity: entity?.entity_id ?? "" };
  }

  static getConfigElement(): HTMLElement {
    return document.createElement(`${CARD_TYPE}-editor`);
  }

  setConfig(config: unknown): void {
    this._config = validateConfig(config, this.language);
  }

  getCardSize(): number {
    const count = this._config ? this.shipments().length : 0;
    const shown = Math.min(count, this._config?.max_items ?? count);
    return 1 + Math.max(1, shown * 2);
  }

  getGridOptions(): { columns: number; min_rows: number } {
    return { columns: 12, min_rows: 2 };
  }

  override connectedCallback(): void {
    super.connectedCallback();
    this._timer = setInterval(() => {
      this._tick += 1;
    }, 60_000);
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  /**
   * `hass` wird bei jeder Zustandsaenderung im ganzen System neu gesetzt. Neu
   * gerendert wird nur, wenn sich die eigene Entity oder die Sprache aendert —
   * oder die Karte selbst (Aufklappen, Minutentakt).
   */
  protected override shouldUpdate(changed: PropertyValues<this>): boolean {
    if (!this._config) {
      return false;
    }
    if (changed.has("_config") || changed.has("_expanded") || changed.has("_tick")) {
      return true;
    }
    const oldHass = changed.get("hass") as HomeAssistant | undefined;
    if (!oldHass || !this.hass) {
      return changed.has("hass");
    }
    const { entity } = this._config;
    return (
      oldHass.states[entity] !== this.hass.states[entity] ||
      oldHass.locale.language !== this.hass.locale.language ||
      oldHass.config?.time_zone !== this.hass.config?.time_zone
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
      <div class="card-content">${this.renderBody(this._config, stateObj)}</div>
    </ha-card>`;
  }

  private shipments(): Shipment[] {
    const stateObj = this._config ? this.hass?.states[this._config.entity] : undefined;
    return parseShipments(stateObj?.attributes.shipments);
  }

  private renderBody(
    config: DeliveryTrackerCardConfig,
    stateObj: HassEntity | undefined,
  ): TemplateResult {
    const { entity } = config;
    if (!stateObj) {
      return this.renderWarning("error.entity_not_found", entity);
    }
    if (stateObj.state === "unavailable") {
      return this.renderWarning("error.entity_unavailable", entity);
    }
    if (!Array.isArray(stateObj.attributes.shipments)) {
      return this.renderWarning("error.not_a_shipment_sensor", entity);
    }

    const shipments = parseShipments(stateObj.attributes.shipments);
    const labels = makeLabels(stateObj, this.language);
    const view = buildView(shipments, config, labels);
    if (!view.matching) {
      return this.renderEmpty(shipments.length ? "card.empty_filtered" : "card.empty");
    }

    const ctx: RowContext = {
      fields: new Set(effectiveFields(config)),
      labels,
      format: {
        now: new Date(),
        language: this.language,
        timeZone: this.hass?.config?.time_zone || undefined,
      },
    };
    return html`${view.groups.map((group) => this.renderGroup(group, ctx))}
    ${
      view.hidden
        ? html`<div class="more">
            ${localize(this.language, "card.more", { count: view.hidden })}
          </div>`
        : nothing
    }`;
  }

  private renderGroup(group: ShipmentGroup, ctx: RowContext): TemplateResult {
    const list = html`<ul class="shipments">
      ${group.shipments.map((shipment) => this.renderShipment(shipment, ctx))}
    </ul>`;
    if (group.label === undefined) {
      return list;
    }
    return html`<section class="group">
      <h3 class="group-title">
        <span>${group.label}</span><span class="group-count">${group.shipments.length}</span>
      </h3>
      ${list}
    </section>`;
  }

  private renderShipment(shipment: Shipment, ctx: RowContext): TemplateResult {
    const { fields, labels } = ctx;
    const expanded = this._expanded.has(shipment.id);
    const carrierName = labels.carrier(shipment.carrier);
    const sender = fields.has("sender") ? shipment.sender : undefined;
    const description = fields.has("description") ? shipment.description : undefined;
    // Ueberschrift: Absender, sonst Beschreibung, sonst der Zusteller.
    const headline = sender ?? description ?? carrierName;
    const subline = sender ? description : undefined;
    const tone = progressOf(shipment.status).tone;

    return html`<li class="shipment tone-${tone}">
      <button
        class="row"
        type="button"
        aria-expanded=${expanded ? "true" : "false"}
        @click=${() => this.toggle(shipment.id)}
      >
        ${
          fields.has("carrier")
            ? html`<span class="badge" title=${carrierName}>${carrierIcon(shipment.carrier)}</span>`
            : nothing
        }
        <span class="main">
          <span class="headline">
            <span class="name">${headline}</span>
            ${fields.has("eta") ? this.renderEta(shipment, ctx) : nothing}
          </span>
          ${subline ? html`<span class="subline">${subline}</span>` : nothing}
          ${this.renderMeta(shipment, ctx, carrierName)}
          ${fields.has("progress") ? this.renderProgress(shipment.status, labels) : nothing}
          ${fields.has("pickup") ? this.renderPickup(shipment) : nothing}
        </span>
        ${iconChevron()}
      </button>
      ${expanded ? this.renderDetails(shipment, ctx) : nothing}
    </li>`;
  }

  private renderEta(shipment: Shipment, ctx: RowContext): TemplateResult | typeof nothing {
    const eta = formatEta(shipment, ctx.format);
    if (!eta) {
      return nothing;
    }
    const icon = { allday: iconCalendar, window: iconClock, delivered: iconCheck }[eta.kind];
    return html`<span
      class="eta eta-${eta.kind}"
      title=${localize(this.language, `eta.${eta.kind}`)}
      >${icon()}<span>${eta.text}</span></span
    >`;
  }

  private renderMeta(
    shipment: Shipment,
    { fields, labels }: RowContext,
    carrierName: string,
  ): TemplateResult | typeof nothing {
    const parts: TemplateResult[] = [];
    if (fields.has("status")) {
      parts.push(html`<span class="chip status">${labels.status(shipment.status)}</span>`);
    }
    if (fields.has("carrier")) {
      parts.push(html`<span>${carrierName}</span>`);
    }
    if (fields.has("destination")) {
      parts.push(
        html`<span class="destination">→ ${labels.destination(destinationOf(shipment))}</span>`,
      );
    }
    if (fields.has("tracking_number") && shipment.tracking_number) {
      parts.push(html`<span class="tracking-number">${shipment.tracking_number}</span>`);
    }
    return parts.length ? html`<span class="meta">${parts}</span>` : nothing;
  }

  /** Normaler Verlauf als Stufen; Retour und gescheiterte Zustellung als Ausnahme. */
  private renderProgress(status: ShipmentStatus, labels: Labels): TemplateResult {
    const { step, tone } = progressOf(status);
    const segments = Array.from(
      { length: PROGRESS_STEPS },
      (_, index) => html`<span class="segment ${index < step ? "filled" : ""}"></span>`,
    );
    if (tone === "exception") {
      return html`<span
        class="progress exception"
        role="img"
        aria-label=${localize(this.language, "progress.exception", {
          status: labels.status(status),
        })}
        >${segments}</span
      >`;
    }
    return html`<span
      class="progress"
      role="progressbar"
      aria-valuemin="0"
      aria-valuemax=${PROGRESS_STEPS}
      aria-valuenow=${step}
      aria-valuetext=${localize(this.language, "progress.step", { step, total: PROGRESS_STEPS })}
      >${segments}</span
    >`;
  }

  private renderPickup(shipment: Shipment): TemplateResult | typeof nothing {
    if (!shipment.pickup_location && !shipment.pickup_code) {
      return nothing;
    }
    return html`<span class="pickup">
      ${shipment.pickup_location ? html`<span>${shipment.pickup_location}</span>` : nothing}
      ${
        shipment.pickup_code
          ? html`<span class="pickup-code"
              >${localize(this.language, "detail.pickup_code")}:
              <strong>${shipment.pickup_code}</strong></span
            >`
          : nothing
      }
    </span>`;
  }

  /** Alles, was die Zeile weglaesst oder kuerzt, plus der Link zur Sendungsverfolgung. */
  private renderDetails(shipment: Shipment, { labels, format }: RowContext): TemplateResult {
    const rows: [StringKey, string | undefined][] = [
      ["field.tracking_number", shipment.tracking_number],
      ["field.carrier", labels.carrier(shipment.carrier)],
      ["field.status", labels.status(shipment.status)],
      ["detail.status_text", shipment.status_text],
      ["field.sender", shipment.sender],
      ["field.description", shipment.description],
      ["field.destination", labels.destination(destinationOf(shipment))],
      ["detail.pickup_location", shipment.pickup_location],
      ["detail.pickup_code", shipment.pickup_code],
      ["field.eta", formatEta({ ...shipment, status: "unknown" }, format)?.text],
      ["detail.delivered_at", formatTimestamp(shipment.delivered_at, format)],
      ["detail.last_updated", formatTimestamp(shipment.last_updated, format)],
    ];
    const url = safeTrackingUrl(shipment);
    return html`<div class="details">
      <dl>
        ${rows
          .filter(([, value]) => value)
          .map(
            ([key, value]) =>
              html`<dt>${localize(this.language, key)}</dt>
                <dd>${value}</dd>`,
          )}
      </dl>
      ${
        url
          ? html`<a class="tracking-link" href=${url} target="_blank" rel="noopener noreferrer"
              >${iconExternal()}${localize(this.language, "detail.open_tracking")}</a
            >`
          : nothing
      }
    </div>`;
  }

  private renderEmpty(key: StringKey): TemplateResult {
    return html`<div class="empty">
      ${iconEmpty()}<span>${localize(this.language, key)}</span>
    </div>`;
  }

  private renderWarning(key: StringKey, entity: string): TemplateResult {
    return html`<div class="warning" role="alert">
      ${localize(this.language, key, { entity })}
    </div>`;
  }

  private toggle(id: string): void {
    const expanded = new Set(this._expanded);
    if (!expanded.delete(id)) {
      expanded.add(id);
    }
    this._expanded = expanded;
  }

  private get language(): string | undefined {
    return this.hass?.locale.language ?? globalThis.navigator?.language;
  }

  static override styles = css`
    :host {
      --dt-gap: 12px;
    }
    .card-content {
      padding-top: 0;
    }
    .group + .group {
      margin-top: 8px;
    }
    .group-title {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 8px 0 0;
      font-size: 0.85em;
      font-weight: 500;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--secondary-text-color);
    }
    .group-count {
      min-width: 1.5em;
      padding: 0 6px;
      border-radius: 999px;
      text-align: center;
      background: var(--secondary-background-color);
    }
    .shipments {
      list-style: none;
      margin: 0;
      padding: 0;
    }
    .shipment + .shipment {
      border-top: 1px solid var(--divider-color);
    }
    .row {
      all: unset;
      box-sizing: border-box;
      display: flex;
      align-items: flex-start;
      gap: var(--dt-gap);
      width: 100%;
      padding: 10px 0;
      cursor: pointer;
      color: var(--primary-text-color);
    }
    .row:focus-visible {
      outline: 2px solid var(--primary-color);
      outline-offset: 2px;
      border-radius: 4px;
    }
    .badge {
      flex: none;
      display: flex;
      width: 40px;
      height: 40px;
      border-radius: 10px;
      background: var(--secondary-background-color);
      color: var(--primary-text-color);
    }
    .carrier-icon {
      width: 100%;
      height: 100%;
    }
    .main {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .headline {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      justify-content: space-between;
      column-gap: var(--dt-gap);
      row-gap: 2px;
    }
    .name {
      font-weight: 500;
      overflow-wrap: anywhere;
    }
    .subline,
    .meta,
    .pickup {
      font-size: 0.9em;
      color: var(--secondary-text-color);
      overflow-wrap: anywhere;
    }
    .meta,
    .pickup {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px 10px;
    }
    .chip {
      padding: 0 8px;
      border-radius: 999px;
      background: var(--secondary-background-color);
      color: var(--primary-text-color);
    }
    .tone-warning .chip.status {
      background: color-mix(in srgb, var(--warning-color) 20%, transparent);
    }
    .tone-exception .chip.status {
      background: color-mix(in srgb, var(--error-color) 20%, transparent);
    }
    .tone-done .chip.status {
      background: color-mix(in srgb, var(--success-color) 20%, transparent);
    }
    .tracking-number {
      font-family: var(--code-font-family, monospace);
    }
    .pickup-code strong {
      font-family: var(--code-font-family, monospace);
      color: var(--primary-text-color);
    }
    .eta {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 0.9em;
      white-space: nowrap;
    }
    .eta-allday {
      color: var(--secondary-text-color);
    }
    .eta-window {
      color: var(--primary-color);
      font-weight: 500;
    }
    .eta-delivered {
      color: var(--success-color);
    }
    .icon {
      flex: none;
      width: 18px;
      height: 18px;
    }
    .chevron {
      align-self: center;
      color: var(--secondary-text-color);
      transition: transform 0.2s;
    }
    .row[aria-expanded="true"] .chevron {
      transform: rotate(180deg);
    }
    .progress {
      display: flex;
      gap: 3px;
      margin-top: 2px;
    }
    .segment {
      flex: 1;
      height: 4px;
      border-radius: 2px;
      background: var(--divider-color);
    }
    .segment.filled {
      background: var(--primary-color);
    }
    .tone-warning .segment.filled {
      background: var(--warning-color);
    }
    .tone-done .segment.filled {
      background: var(--success-color);
    }
    /* Ausnahme: kein Fortschritt, sondern ein durchgehend schraffierter Balken. */
    .progress.exception {
      gap: 0;
    }
    .progress.exception .segment {
      border-radius: 0;
      background: repeating-linear-gradient(-45deg, var(--error-color) 0 4px, transparent 4px 8px);
    }
    .progress.exception .segment:first-child {
      border-radius: 2px 0 0 2px;
    }
    .progress.exception .segment:last-child {
      border-radius: 0 2px 2px 0;
    }
    .details {
      margin: 0 0 12px calc(40px + var(--dt-gap));
      font-size: 0.9em;
    }
    .details dl {
      display: grid;
      grid-template-columns: minmax(0, max-content) minmax(0, 1fr);
      gap: 4px 12px;
      margin: 0;
    }
    .details dt {
      color: var(--secondary-text-color);
    }
    .details dd {
      margin: 0;
      overflow-wrap: anywhere;
    }
    .tracking-link {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-top: 8px;
      color: var(--primary-color);
      text-decoration: none;
      font-weight: 500;
    }
    .tracking-link:hover {
      text-decoration: underline;
    }
    .more {
      padding-top: 8px;
      text-align: center;
      font-size: 0.9em;
      color: var(--secondary-text-color);
    }
    .empty {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 8px;
      padding: 16px 0 8px;
      color: var(--secondary-text-color);
    }
    .empty-icon {
      width: 48px;
      height: 48px;
      opacity: 0.6;
    }
    .warning {
      padding: 8px 12px;
      border-radius: var(--ha-card-border-radius, 12px);
      border-left: 4px solid var(--warning-color);
      background: color-mix(in srgb, var(--warning-color) 12%, transparent);
      color: var(--primary-text-color);
      overflow-wrap: anywhere;
    }
    /* Schmale Karten (Mobilgeraet, schmale Spalte): Details untereinander. */
    @media (max-width: 450px) {
      .details {
        margin-left: 0;
      }
      .details dl {
        grid-template-columns: minmax(0, 1fr);
      }
      .details dd {
        margin-bottom: 4px;
      }
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
