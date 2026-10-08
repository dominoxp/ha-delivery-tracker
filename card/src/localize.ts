/**
 * Texte der Karte in den Sprachen, die auch die Integration mitbringt.
 * Unbekannte Sprachen fallen auf Englisch zurueck.
 */
const STRINGS = {
  en: {
    "card.name": "Delivery Tracker",
    "card.description": "Shows the shipments tracked by the Delivery Tracker integration.",
    "card.default_title": "Shipments",
    "card.count": "{count} shipment(s)",
    "config.not_an_object": "The card configuration must be an object.",
    "config.missing_entity": "Please set `entity` to a Delivery Tracker sensor.",
    "config.invalid_entity":
      "`entity` must be a sensor entity ID such as `sensor.delivery_tracker_active_shipments`, got `{entity}`.",
    "config.invalid_title": "`title` must be text.",
    "error.entity_not_found": "Entity `{entity}` not found.",
    "error.entity_unavailable": "Entity `{entity}` is unavailable.",
    "error.not_a_shipment_sensor":
      "Entity `{entity}` is not a Delivery Tracker sensor (no `shipments` attribute).",
  },
  de: {
    "card.name": "Delivery Tracker",
    "card.description": "Zeigt die Sendungen der Delivery-Tracker-Integration.",
    "card.default_title": "Sendungen",
    "card.count": "{count} Sendung(en)",
    "config.not_an_object": "Die Kartenkonfiguration muss ein Objekt sein.",
    "config.missing_entity":
      "Bitte unter `entity` einen Sensor der Delivery-Tracker-Integration angeben.",
    "config.invalid_entity":
      "`entity` muss eine Sensor-Entity-ID wie `sensor.delivery_tracker_aktive_sendungen` sein, erhalten: `{entity}`.",
    "config.invalid_title": "`title` muss ein Text sein.",
    "error.entity_not_found": "Entity `{entity}` nicht gefunden.",
    "error.entity_unavailable": "Entity `{entity}` ist nicht verfügbar.",
    "error.not_a_shipment_sensor":
      "Entity `{entity}` ist kein Sensor der Delivery-Tracker-Integration (kein Attribut `shipments`).",
  },
} as const;

export type StringKey = keyof (typeof STRINGS)["en"];

export function localize(
  language: string | undefined,
  key: StringKey,
  vars: Record<string, string | number> = {},
): string {
  const base = language?.split("-")[0];
  const table = base === "de" ? STRINGS.de : STRINGS.en;
  return table[key].replace(/\{(\w+)\}/g, (match, name: string) =>
    name in vars ? String(vars[name]) : match,
  );
}
