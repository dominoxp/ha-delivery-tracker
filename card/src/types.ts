/**
 * Der Ausschnitt des `hass`-Objekts, den die Karte liest.
 *
 * Bewusst selbst definiert statt aus einem Fremdpaket: Es sind nur die stabilen,
 * oeffentlich dokumentierten Felder, die jede Custom-Card erhaelt.
 */
export interface HassEntity {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown> & { friendly_name?: string };
  last_changed: string;
  last_updated: string;
}

export interface HomeAssistant {
  states: Record<string, HassEntity | undefined>;
  locale: { language: string };
}

/** Eintrag fuer die Kartenauswahl der Oberflaeche. */
export interface CustomCardEntry {
  type: string;
  name: string;
  description?: string;
  preview?: boolean;
  documentationURL?: string;
}

declare global {
  interface Window {
    customCards?: CustomCardEntry[];
  }
}
