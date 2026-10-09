import { localize } from "./localize";
import { STATUSES, type ShipmentStatus } from "./shipment";

export const CARD_TYPE = "delivery-tracker-card";

/** Darstellbare Felder je Sendung, in der Reihenfolge der Anzeige. */
export const FIELDS = [
  "carrier",
  "status",
  "progress",
  "eta",
  "sender",
  "description",
  "destination",
  "pickup",
  "tracking_number",
] as const;
export type Field = (typeof FIELDS)[number];

export const GROUP_BY = ["none", "destination", "status", "carrier"] as const;
export type GroupBy = (typeof GROUP_BY)[number];

export const SORT_BY = ["eta", "status", "last_updated", "carrier"] as const;
export type SortBy = (typeof SORT_BY)[number];

export const DEFAULT_GROUP_BY: GroupBy = "none";
export const DEFAULT_SORT_BY: SortBy = "eta";

export interface DeliveryTrackerCardConfig {
  type: string;
  /** Ein Sensor der Integration mit `shipments`-Attribut. */
  entity: string;
  title?: string;
  /** Filter: nur diese Zielorte (`unknown` = ohne Zuordnung). Fehlt = alle. */
  destinations?: string[];
  /** Filter: nur diese Status. Fehlt = alle. */
  statuses?: ShipmentStatus[];
  /** Filter: nur diese Zusteller. Fehlt = alle. */
  carriers?: string[];
  group_by?: GroupBy;
  sort_by?: SortBy;
  /** Hoechstzahl angezeigter Sendungen. Fehlt = alle. */
  max_items?: number;
  /** Angezeigte Felder. Fehlt = alle. */
  fields?: Field[];
}

/** Die Optionen dieser Karte; alles andere (z.B. `grid_options`) gehoert HA. */
const OWN_KEYS = [
  "type",
  "entity",
  "title",
  "destinations",
  "statuses",
  "carriers",
  "group_by",
  "sort_by",
  "max_items",
  "fields",
] as const;

const SENSOR_ENTITY_ID = /^sensor\.[a-z0-9_]+$/;

/** Wirksame Optionen: Standardwerte eingesetzt. */
export function effectiveFields(config: DeliveryTrackerCardConfig): readonly Field[] {
  return config.fields ?? FIELDS;
}

/**
 * Prueft die Konfiguration aus dem Dashboard und wirft bei Fehlern eine lesbare
 * Meldung. HA zeigt die Meldung eines in `setConfig` geworfenen Fehlers als
 * Fehlerkarte an — eine leere Flaeche entsteht so nie.
 *
 * Das Ergebnis ist zugleich die Normalform, die auch der Editor schreibt:
 * Standardwerte und leere Listen fallen weg, Status und Felder stehen in
 * fester Reihenfolge, Doppelte entfallen. Fremde Schluessel (von HA, etwa
 * `grid_options` oder `visibility`) bleiben erhalten und stehen am Ende.
 */
export function validateConfig(raw: unknown, language?: string): DeliveryTrackerCardConfig {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error(localize(language, "config.not_an_object"));
  }
  const config = raw as Record<string, unknown>;

  const entity = requireEntity(config.entity, language);
  const { title } = config;
  if (title !== undefined && typeof title !== "string") {
    throw new Error(localize(language, "config.invalid_title"));
  }

  const destinations = stringList(config, "destinations", language);
  const carriers = stringList(config, "carriers", language);
  const statuses = choiceList(config, "statuses", STATUSES, language);
  const fields = choiceList(config, "fields", FIELDS, language);
  const groupBy = choice(config, "group_by", GROUP_BY, language);
  const sortBy = choice(config, "sort_by", SORT_BY, language);
  const maxItems = positiveInt(config, "max_items", language);

  const result: DeliveryTrackerCardConfig = {
    type: typeof config.type === "string" ? config.type : `custom:${CARD_TYPE}`,
    entity,
  };
  if (title) {
    result.title = title;
  }
  if (destinations.length) {
    result.destinations = destinations;
  }
  if (statuses.length) {
    result.statuses = statuses;
  }
  if (carriers.length) {
    result.carriers = carriers;
  }
  if (groupBy && groupBy !== DEFAULT_GROUP_BY) {
    result.group_by = groupBy;
  }
  if (sortBy && sortBy !== DEFAULT_SORT_BY) {
    result.sort_by = sortBy;
  }
  if (maxItems !== undefined) {
    result.max_items = maxItems;
  }
  // Alle Felder ausgewaehlt ist dasselbe wie keine Angabe.
  if (fields.length && fields.length < FIELDS.length) {
    result.fields = fields;
  }

  const foreign = Object.fromEntries(
    Object.entries(config).filter(([key]) => !(OWN_KEYS as readonly string[]).includes(key)),
  );
  return { ...result, ...foreign };
}

function requireEntity(entity: unknown, language?: string): string {
  if (entity === undefined || entity === null || entity === "") {
    throw new Error(localize(language, "config.missing_entity"));
  }
  if (typeof entity !== "string" || !SENSOR_ENTITY_ID.test(entity)) {
    throw new Error(localize(language, "config.invalid_entity", { entity: String(entity) }));
  }
  return entity;
}

function stringList(config: Record<string, unknown>, key: string, language?: string): string[] {
  const value = config[key];
  if (value === undefined || value === null) {
    return [];
  }
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || item === "")) {
    throw new Error(localize(language, "config.invalid_list", { key }));
  }
  return [...new Set(value as string[])];
}

/** Liste aus einer festen Auswahl; Ergebnis in der Reihenfolge der Auswahl. */
function choiceList<T extends string>(
  config: Record<string, unknown>,
  key: string,
  choices: readonly T[],
  language?: string,
): T[] {
  const values = stringList(config, key, language);
  const unknown = values.find((value) => !(choices as readonly string[]).includes(value));
  if (unknown !== undefined) {
    throw new Error(
      localize(language, "config.invalid_value", {
        key,
        value: unknown,
        choices: choices.join(", "),
      }),
    );
  }
  return choices.filter((item) => values.includes(item));
}

function choice<T extends string>(
  config: Record<string, unknown>,
  key: string,
  choices: readonly T[],
  language?: string,
): T | undefined {
  const value = config[key];
  if (value === undefined || value === null) {
    return undefined;
  }
  if (typeof value !== "string" || !(choices as readonly string[]).includes(value)) {
    throw new Error(
      localize(language, "config.invalid_choice", {
        key,
        value: String(value),
        choices: choices.join(", "),
      }),
    );
  }
  return value as T;
}

function positiveInt(
  config: Record<string, unknown>,
  key: string,
  language?: string,
): number | undefined {
  const value = config[key];
  if (value === undefined || value === null) {
    return undefined;
  }
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
    throw new Error(localize(language, "config.invalid_max_items"));
  }
  return value;
}
