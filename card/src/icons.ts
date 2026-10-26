/**
 * Mitgelieferte Symbole — nichts wird von fremden Servern geladen.
 *
 * Zusteller erscheinen als Kuerzel-Plakette statt mit ihrem Logo: Logos sind
 * geschuetzte Marken und brauchen feste Farben, die Karte nutzt aber nur
 * Theme-Farben (`currentColor`).
 */
import { html, svg, type SVGTemplateResult, type TemplateResult } from "lit";

const MONOGRAMS: Record<string, string> = {
  dhl: "DHL",
  aliexpress: "AE",
  amazon: "a",
  dpd: "DPD",
  gls: "GLS",
  hermes: "H",
  ups: "UPS",
};

/** Ein Paket — fuer den generischen Fallback, unbekannte Zusteller und die leere Liste. */
const PACKAGE = svg`<path
  d="M20 6 33 12.5v15L20 34 7 27.5v-15ZM7 12.5 20 19l13-6.5M20 19v15M13.5 9.2l13 6.6"
  fill="none"
  stroke="currentColor"
  stroke-width="2.2"
  stroke-linejoin="round"
/>`;

function icon(viewBox: string, content: SVGTemplateResult, cls = "icon"): TemplateResult {
  return html`<svg class=${cls} viewBox=${viewBox} aria-hidden="true" focusable="false">
    ${content}
  </svg>`;
}

function line(d: string, width = 1.8): SVGTemplateResult {
  return svg`<path d=${d} fill="none" stroke="currentColor" stroke-width=${width}
    stroke-linecap="round" stroke-linejoin="round" />`;
}

export function carrierIcon(carrier: string): TemplateResult {
  const monogram = MONOGRAMS[carrier];
  if (!monogram) {
    return icon("0 0 40 40", PACKAGE, "carrier-icon");
  }
  // Je laenger das Kuerzel, desto kleiner die Schrift.
  const size = monogram.length > 2 ? 12 : 18;
  return icon(
    "0 0 40 40",
    svg`<text x="20" y="20" text-anchor="middle" dominant-baseline="central"
      font-size=${size} font-weight="700" fill="currentColor">${monogram}</text>`,
    "carrier-icon",
  );
}

/** Kalenderblatt: Termin ohne Uhrzeit. */
export const iconCalendar = (): TemplateResult =>
  icon("0 0 24 24", line("M4 6.5h16v13H4zM4 10.5h16M8 4v4M16 4v4"));

/** Uhr: Zeitfenster. */
export const iconClock = (): TemplateResult =>
  icon(
    "0 0 24 24",
    svg`<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.8" />
      ${line("M12 7.5V12l3 2")}`,
  );

/** Haken: zugestellt. */
export const iconCheck = (): TemplateResult => icon("0 0 24 24", line("M5 12.5 10 17.5 19 7", 2));

/** Pfeil fuer aufgeklappte/zugeklappte Details. */
export const iconChevron = (): TemplateResult =>
  icon("0 0 24 24", line("M8 10l4 4 4-4", 2), "icon chevron");

/** Externer Link. */
export const iconExternal = (): TemplateResult =>
  icon("0 0 24 24", line("M14 4h6v6M20 4l-9 9M18 14v5H5V6h5"));

/** Leere Liste. */
export const iconEmpty = (): TemplateResult => icon("0 0 40 40", PACKAGE, "empty-icon");
