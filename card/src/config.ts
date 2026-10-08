import { localize } from "./localize";

export const CARD_TYPE = "delivery-tracker-card";

export interface DeliveryTrackerCardConfig {
  type: string;
  /** Ein Sensor der Integration mit `shipments`-Attribut. */
  entity: string;
  title?: string;
}

const SENSOR_ENTITY_ID = /^sensor\.[a-z0-9_]+$/;

/**
 * Prueft die Konfiguration aus dem Dashboard und wirft bei Fehlern eine lesbare
 * Meldung. HA zeigt die Meldung eines in `setConfig` geworfenen Fehlers als
 * Fehlerkarte an — eine leere Flaeche entsteht so nie.
 */
export function validateConfig(raw: unknown, language?: string): DeliveryTrackerCardConfig {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error(localize(language, "config.not_an_object"));
  }
  const config = raw as Record<string, unknown>;

  const { entity, title } = config;
  if (entity === undefined || entity === null || entity === "") {
    throw new Error(localize(language, "config.missing_entity"));
  }
  if (typeof entity !== "string" || !SENSOR_ENTITY_ID.test(entity)) {
    throw new Error(localize(language, "config.invalid_entity", { entity: String(entity) }));
  }
  if (title !== undefined && typeof title !== "string") {
    throw new Error(localize(language, "config.invalid_title"));
  }

  return {
    type: typeof config.type === "string" ? config.type : `custom:${CARD_TYPE}`,
    entity,
    ...(title !== undefined && { title }),
  };
}
