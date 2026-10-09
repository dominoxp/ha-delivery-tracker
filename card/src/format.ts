/**
 * Zeitangaben lesbar machen: „heute 14–18 Uhr", „morgen", „Dienstag".
 *
 * Gerechnet wird in der HA-Zeitzone, ausgegeben in der Sprache des Nutzers.
 * Die Integration legt einen Termin ohne Uhrzeit als Mitternacht bis
 * Mitternacht in der HA-Zeitzone ab (`dates.parse_eta`) — daran erkennt die
 * Karte ganztaegige Angaben.
 */
import type { Shipment } from "./shipment";

export interface FormatContext {
  now: Date;
  language?: string;
  /** IANA-Zeitzone von HA; ohne Angabe die des Browsers. */
  timeZone?: string;
}

export type EtaKind = "allday" | "window" | "delivered";

export interface EtaLabel {
  kind: EtaKind;
  text: string;
}

interface ZonedTime {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

const DAY_MS = 86_400_000;

const partFormatters = new Map<string, Intl.DateTimeFormat>();

/** Kalenderdatum und Uhrzeit eines Zeitpunkts in der gegebenen Zeitzone. */
function zoned(date: Date, timeZone: string | undefined): ZonedTime {
  const key = timeZone ?? "";
  let formatter = partFormatters.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      second: "numeric",
    });
    partFormatters.set(key, formatter);
  }
  const parts: Record<string, number> = {};
  for (const part of formatter.formatToParts(date)) {
    if (part.type !== "literal") {
      parts[part.type] = Number(part.value);
    }
  }
  return {
    year: parts.year,
    month: parts.month,
    day: parts.day,
    hour: parts.hour % 24,
    minute: parts.minute,
    second: parts.second,
  };
}

function isMidnight(time: ZonedTime): boolean {
  return time.hour === 0 && time.minute === 0 && time.second === 0;
}

/** Abstand in Kalendertagen der Zeitzone; positiv, wenn `date` nach `reference` liegt. */
export function dayDiff(date: Date, reference: Date, timeZone?: string): number {
  const a = zoned(date, timeZone);
  const b = zoned(reference, timeZone);
  return Math.round(
    (Date.UTC(a.year, a.month - 1, a.day) - Date.UTC(b.year, b.month - 1, b.day)) / DAY_MS,
  );
}

/** „heute", „morgen", „Dienstag", „Di., 20. Okt." — je nach Abstand zu heute. */
export function formatDay(date: Date, ctx: FormatContext): string {
  const diff = dayDiff(date, ctx.now, ctx.timeZone);
  if (Math.abs(diff) <= 1) {
    return new Intl.RelativeTimeFormat(ctx.language, { numeric: "auto" }).format(diff, "day");
  }
  if (diff > 1 && diff < 7) {
    return new Intl.DateTimeFormat(ctx.language, {
      timeZone: ctx.timeZone,
      weekday: "long",
    }).format(date);
  }
  const sameYear = zoned(date, ctx.timeZone).year === zoned(ctx.now, ctx.timeZone).year;
  return new Intl.DateTimeFormat(ctx.language, {
    timeZone: ctx.timeZone,
    weekday: "short",
    day: "numeric",
    month: "short",
    ...(!sameYear && { year: "numeric" }),
  }).format(date);
}

/** „14 Uhr", „14:30 Uhr", „14–18 Uhr" — Minuten nur, wenn sie etwas aussagen. */
export function formatTimeRange(start: Date, end: Date | undefined, ctx: FormatContext): string {
  const withMinutes = [start, end].some((date) => date && zoned(date, ctx.timeZone).minute !== 0);
  const formatter = new Intl.DateTimeFormat(ctx.language, {
    timeZone: ctx.timeZone,
    hour: "numeric",
    ...(withMinutes && { minute: "2-digit" }),
  });
  if (!end || end.getTime() <= start.getTime()) {
    return formatter.format(start);
  }
  return formatter.formatRange(start, end);
}

function parseDate(value: string | undefined): Date | undefined {
  if (!value) {
    return undefined;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

/** True, wenn das Fenster ganze Tage der Zeitzone umfasst (Termin ohne Uhrzeit). */
export function isWholeDays(start: Date, end: Date | undefined, timeZone?: string): boolean {
  return (
    end !== undefined &&
    end.getTime() > start.getTime() &&
    isMidnight(zoned(start, timeZone)) &&
    isMidnight(zoned(end, timeZone))
  );
}

/**
 * Die erwartete (oder erfolgte) Zustellung als lesbarer Text.
 *
 * - zugestellt: Tag und, wenn bekannt, Uhrzeit der Zustellung;
 * - ganztaegig: nur der Tag, ueber mehrere Tage „Montag – Mittwoch";
 * - Zeitfenster: Tag und Uhrzeiten.
 *
 * Ohne Termin `undefined`.
 */
export function formatEta(shipment: Shipment, ctx: FormatContext): EtaLabel | undefined {
  const delivered = parseDate(shipment.delivered_at);
  if (shipment.status === "delivered" && delivered) {
    const day = formatDay(delivered, ctx);
    const text = isMidnight(zoned(delivered, ctx.timeZone))
      ? day
      : `${day} ${formatTimeRange(delivered, undefined, ctx)}`;
    return { kind: "delivered", text };
  }

  const start = parseDate(shipment.eta_start);
  if (!start) {
    return undefined;
  }
  const end = parseDate(shipment.eta_end);

  if (end && isWholeDays(start, end, ctx.timeZone)) {
    // Das Ende ist Mitternacht des Folgetags; der letzte Tag liegt davor.
    const lastDay = new Date(end.getTime() - DAY_MS / 2);
    const first = formatDay(start, ctx);
    if (dayDiff(lastDay, start, ctx.timeZone) === 0) {
      return { kind: "allday", text: first };
    }
    return { kind: "allday", text: `${first} – ${formatDay(lastDay, ctx)}` };
  }

  if (!end || dayDiff(end, start, ctx.timeZone) === 0) {
    return {
      kind: "window",
      text: `${formatDay(start, ctx)} ${formatTimeRange(start, end, ctx)}`,
    };
  }
  // Ein Fenster ueber Mitternacht: beide Enden mit Tag.
  return {
    kind: "window",
    text:
      `${formatDay(start, ctx)} ${formatTimeRange(start, undefined, ctx)} – ` +
      `${formatDay(end, ctx)} ${formatTimeRange(end, undefined, ctx)}`,
  };
}

/** Zeitstempel fuer die Detailansicht: „heute 14:32", „Di., 20. Okt. 9:05". */
export function formatTimestamp(value: string | undefined, ctx: FormatContext): string | undefined {
  const date = parseDate(value);
  if (!date) {
    return undefined;
  }
  const time = new Intl.DateTimeFormat(ctx.language, {
    timeZone: ctx.timeZone,
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
  return `${formatDay(date, ctx)} ${time}`;
}
