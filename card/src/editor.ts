import { LitElement, css, html, nothing, type TemplateResult } from "lit";

import {
  CARD_TYPE,
  DEFAULT_GROUP_BY,
  DEFAULT_SORT_BY,
  FIELDS,
  GROUP_BY,
  SORT_BY,
  effectiveFields,
  validateConfig,
  type DeliveryTrackerCardConfig,
} from "./config";
import { makeLabels } from "./labels";
import { localize, type StringKey } from "./localize";
import { DESTINATION_UNKNOWN, STATUSES, parseShipments } from "./shipment";
import type { HassEntity, HomeAssistant } from "./types";

type RawConfig = Record<string, unknown>;

interface Option {
  value: string;
  label: string;
}

/**
 * Visueller Editor fuer alle Optionen der Karte.
 *
 * Bewusst aus nativen Formularelementen statt aus `ha-form`: Die internen
 * Komponenten des HA-Frontends sind keine stabile Schnittstelle. Der Editor
 * meldet jede Aenderung als `config-changed` — mit der Konfiguration in
 * derselben Normalform, die `validateConfig` aus einer von Hand
 * geschriebenen erzeugt.
 */
export class DeliveryTrackerCardEditor extends LitElement {
  static override properties = {
    hass: { attribute: false },
    _config: { state: true },
  };

  declare hass?: HomeAssistant;
  declare _config?: RawConfig;

  setConfig(config: DeliveryTrackerCardConfig | RawConfig): void {
    this._config = { ...config };
  }

  protected override render(): TemplateResult | typeof nothing {
    if (!this._config) {
      return nothing;
    }
    const config = this._config;
    const entity = typeof config.entity === "string" ? config.entity : "";
    const stateObj = this.hass?.states[entity];
    const labels = makeLabels(stateObj, this.language);
    const current = this.current();

    return html`<div class="editor">
      <label class="field">
        <span>${this.t("editor.entity")}</span>
        <select
          name="entity"
          @change=${(ev: Event) => this.set("entity", (ev.target as HTMLSelectElement).value)}
        >
          <option value="" ?selected=${!entity}>${this.t("editor.entity_none")}</option>
          ${this.entityOptions(entity).map(
            (option) =>
              html`<option value=${option.value} ?selected=${option.value === entity}>
                ${option.label}
              </option>`,
          )}
        </select>
      </label>

      <label class="field">
        <span>${this.t("editor.title")}</span>
        <input
          name="title"
          type="text"
          .value=${typeof config.title === "string" ? config.title : ""}
          placeholder=${stateObj?.attributes.friendly_name ?? this.t("editor.title_placeholder")}
          @input=${(ev: Event) =>
            this.set("title", (ev.target as HTMLInputElement).value || undefined)}
        />
      </label>

      <h3>${this.t("editor.filter")}</h3>
      <p class="hint">${this.t("editor.filter_hint")}</p>
      ${this.renderChecks(
        "destinations",
        this.t("editor.destinations"),
        this.destinationOptions(stateObj, current.destinations, labels.destination),
        current.destinations ?? [],
      )}
      ${this.renderChecks(
        "statuses",
        this.t("editor.statuses"),
        STATUSES.map((status) => ({ value: status, label: labels.status(status) })),
        current.statuses ?? [],
      )}
      ${this.renderChecks(
        "carriers",
        this.t("editor.carriers"),
        this.carrierOptions(stateObj, current.carriers, labels.carrier),
        current.carriers ?? [],
      )}

      <h3>${this.t("editor.layout")}</h3>
      <div class="row">
        ${this.renderSelect(
          "group_by",
          this.t("editor.group_by"),
          GROUP_BY.map((value) => ({ value, label: this.t(`group.${value}`) })),
          current.group_by ?? DEFAULT_GROUP_BY,
        )}
        ${this.renderSelect(
          "sort_by",
          this.t("editor.sort_by"),
          SORT_BY.map((value) => ({ value, label: this.t(`sort.${value}`) })),
          current.sort_by ?? DEFAULT_SORT_BY,
        )}
        <label class="field">
          <span>${this.t("editor.max_items")}</span>
          <input
            name="max_items"
            type="number"
            min="1"
            step="1"
            inputmode="numeric"
            placeholder=${this.t("editor.max_items_placeholder")}
            .value=${current.max_items === undefined ? "" : String(current.max_items)}
            @change=${(ev: Event) => this.setMaxItems((ev.target as HTMLInputElement).value)}
          />
        </label>
      </div>
      ${this.renderFieldChecks(current)}
    </div>`;
  }

  /**
   * Die Konfiguration, wie die Karte sie versteht. Ist sie gerade ungueltig
   * (etwa noch kein Sensor gewaehlt), zaehlen die Rohwerte.
   */
  private current(): Partial<DeliveryTrackerCardConfig> {
    try {
      return validateConfig(this._config, this.language);
    } catch {
      return this._config as Partial<DeliveryTrackerCardConfig>;
    }
  }

  private renderSelect(
    key: string,
    label: string,
    options: Option[],
    selected: string,
  ): TemplateResult {
    return html`<label class="field">
      <span>${label}</span>
      <select
        name=${key}
        @change=${(ev: Event) => this.set(key, (ev.target as HTMLSelectElement).value)}
      >
        ${options.map(
          (option) =>
            html`<option value=${option.value} ?selected=${option.value === selected}>
              ${option.label}
            </option>`,
        )}
      </select>
    </label>`;
  }

  private renderChecks(
    key: "destinations" | "statuses" | "carriers",
    legend: string,
    options: Option[],
    selected: readonly string[],
  ): TemplateResult {
    return html`<fieldset name=${key}>
      <legend>${legend}</legend>
      <div class="checks">
        ${options.map(
          (option) =>
            html`<label class="check">
              <input
                type="checkbox"
                value=${option.value}
                .checked=${selected.includes(option.value)}
                @change=${(ev: Event) =>
                  this.toggle(key, option.value, (ev.target as HTMLInputElement).checked)}
              />
              <span>${option.label}</span>
            </label>`,
        )}
      </div>
    </fieldset>`;
  }

  private renderFieldChecks(current: Partial<DeliveryTrackerCardConfig>): TemplateResult {
    const shown = effectiveFields({ type: "", entity: "", ...current });
    return html`<fieldset name="fields">
      <legend>${this.t("editor.fields")}</legend>
      <div class="checks">
        ${FIELDS.map(
          (field) =>
            html`<label class="check">
              <input
                type="checkbox"
                value=${field}
                .checked=${shown.includes(field)}
                ?disabled=${shown.length === 1 && shown.includes(field)}
                @change=${(ev: Event) => {
                  const checked = (ev.target as HTMLInputElement).checked;
                  const next = FIELDS.filter((item) =>
                    item === field ? checked : shown.includes(item),
                  );
                  // Alle Felder ist der Standard und wird nicht geschrieben.
                  this.set("fields", next.length === FIELDS.length ? undefined : next);
                }}
              />
              <span>${this.t(`field.${field}`)}</span>
            </label>`,
        )}
      </div>
    </fieldset>`;
  }

  /** Sensoren der Integration: alle mit Sendungsliste, plus der gewaehlte. */
  private entityOptions(selected: string): Option[] {
    const states = Object.values(this.hass?.states ?? {}).filter(
      (state): state is HassEntity =>
        !!state &&
        state.entity_id.startsWith("sensor.") &&
        Array.isArray(state.attributes.shipments),
    );
    const options = states
      .map((state) => ({
        value: state.entity_id,
        label: state.attributes.friendly_name
          ? `${state.attributes.friendly_name} (${state.entity_id})`
          : state.entity_id,
      }))
      .sort((a, b) => a.label.localeCompare(b.label));
    if (selected && !options.some((option) => option.value === selected)) {
      options.unshift({ value: selected, label: selected });
    }
    return options;
  }

  /** Konfigurierte Zielorte, „unbekannt" und bereits gefilterte, die es nicht mehr gibt. */
  private destinationOptions(
    stateObj: HassEntity | undefined,
    selected: readonly string[] | undefined,
    label: (id: string) => string,
  ): Option[] {
    const ids = new Set(Object.keys(objectOrEmpty(stateObj?.attributes.destinations)));
    ids.add(DESTINATION_UNKNOWN);
    selected?.forEach((id) => ids.add(id));
    return [...ids].map((value) => ({ value, label: label(value) }));
  }

  /** Aktive Zusteller, Zusteller der aktuellen Sendungen und bereits gefilterte. */
  private carrierOptions(
    stateObj: HassEntity | undefined,
    selected: readonly string[] | undefined,
    label: (id: string) => string,
  ): Option[] {
    const ids = new Set(Object.keys(objectOrEmpty(stateObj?.attributes.carriers)));
    parseShipments(stateObj?.attributes.shipments).forEach((shipment) => ids.add(shipment.carrier));
    selected?.forEach((id) => ids.add(id));
    return [...ids]
      .map((value) => ({ value, label: label(value) }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }

  private toggle(key: "destinations" | "statuses" | "carriers", value: string, on: boolean): void {
    const raw = this._config?.[key];
    const list = Array.isArray(raw) ? raw.filter((item) => item !== value) : [];
    if (on) {
      list.push(value);
    }
    this.set(key, list.length ? list : undefined);
  }

  private setMaxItems(value: string): void {
    const parsed = Number.parseInt(value, 10);
    this.set("max_items", Number.isInteger(parsed) && parsed > 0 ? parsed : undefined);
  }

  /** Eine Option setzen (`undefined` entfernt sie) und die Aenderung melden. */
  private set(key: string, value: unknown): void {
    const raw: RawConfig = Object.fromEntries(
      Object.entries({ ...this._config, [key]: value }).filter(([, item]) => item !== undefined),
    );
    let next: RawConfig = raw;
    try {
      next = { ...validateConfig(raw, this.language) };
    } catch {
      // Unvollstaendig (z.B. noch kein Sensor): so weitergeben, die Vorschau
      // zeigt die Meldung der Karte.
    }
    this._config = next;
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: next },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private t(key: StringKey): string {
    return localize(this.language, key);
  }

  private get language(): string | undefined {
    return this.hass?.locale.language ?? globalThis.navigator?.language;
  }

  static override styles = css`
    .editor {
      display: flex;
      flex-direction: column;
      gap: 12px;
      color: var(--primary-text-color);
    }
    h3 {
      margin: 8px 0 0;
      font-size: 1em;
      font-weight: 500;
    }
    .hint {
      margin: -8px 0 0;
      font-size: 0.9em;
      color: var(--secondary-text-color);
    }
    .row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 0.9em;
      color: var(--secondary-text-color);
    }
    select,
    input[type="text"],
    input[type="number"] {
      box-sizing: border-box;
      width: 100%;
      min-height: 40px;
      padding: 8px;
      border: 1px solid var(--divider-color);
      border-radius: 6px;
      background: var(--card-background-color, var(--primary-background-color));
      color: var(--primary-text-color);
      font: inherit;
      font-size: 1rem;
    }
    select:focus,
    input:focus-visible {
      outline: 2px solid var(--primary-color);
      outline-offset: -1px;
    }
    fieldset {
      margin: 0;
      padding: 8px 12px 12px;
      border: 1px solid var(--divider-color);
      border-radius: 6px;
    }
    legend {
      padding: 0 4px;
      font-size: 0.9em;
      color: var(--secondary-text-color);
    }
    .checks {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 4px 12px;
    }
    .check {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 32px;
      cursor: pointer;
    }
    .check input {
      width: 18px;
      height: 18px;
      margin: 0;
      accent-color: var(--primary-color);
    }
  `;
}

function objectOrEmpty(value: unknown): object {
  return typeof value === "object" && value !== null ? value : {};
}

if (!customElements.get(`${CARD_TYPE}-editor`)) {
  customElements.define(`${CARD_TYPE}-editor`, DeliveryTrackerCardEditor);
}

declare global {
  interface HTMLElementTagNameMap {
    "delivery-tracker-card-editor": DeliveryTrackerCardEditor;
  }
}
