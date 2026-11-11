/**
 * Money, rates and hours with integer arithmetic only (ADR 0006): forms enter text, which is
 * parsed with string-integer arithmetic into cents, basis points and hundredths; display splits
 * integers with integer division. No float touches an amount. Shared (server and browser).
 */
const dollars = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** 123456 → "$1,234.56"; null → "—". */
export function formatCents(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "—";
  if (!Number.isSafeInteger(cents)) throw new Error("cents must be a safe integer");
  const neg = cents < 0;
  const abs = Math.abs(cents);
  const whole = Math.trunc(abs / 100);
  const frac = String(abs % 100).padStart(2, "0");
  return `${neg ? "-" : ""}$${dollars.format(whole)}.${frac}`;
}

/** Basis points as "xx.xx%": 2000 → "20.00%"; null → "—". */
export function formatBps(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return "—";
  const whole = Math.trunc(bps / 100);
  const frac = String(Math.abs(bps % 100)).padStart(2, "0");
  return `${whole}.${frac}%`;
}

/** Hundredths of an hour as "h.hh": 8050 → "80.50"; null → "—". */
export function formatHours(hundredths: number | null | undefined): string {
  if (hundredths === null || hundredths === undefined) return "—";
  return `${Math.trunc(hundredths / 100)}.${String(hundredths % 100).padStart(2, "0")}`;
}

const MONEY_RE = /^(\d{1,8})(?:\.(\d{1,2}))?$/;
const PERCENT_RE = /^(\d{1,2})(?:\.(\d{1,2}))?$/;
const HOURS_RE = /^(\d{1,3})(?:\.(\d{1,2}))?$/;

/** "12.5" → 1250 (whole × 100 + two-digit fraction); null when the text doesn't match. */
function parseHundredths(text: string, re: RegExp): number | null {
  const m = re.exec(text.trim());
  if (!m) return null;
  const whole = Number.parseInt(m[1]!, 10);
  const frac = Number.parseInt((m[2] ?? "").padEnd(2, "0"), 10);
  return whole * 100 + frac;
}

/** Money text `^\d{1,8}(\.\d{1,2})?$` → cents (ADR 0006): "1234.5" → 123450. */
export function parseMoney(text: string): number | null {
  return parseHundredths(text, MONEY_RE);
}

/** Percent text `^\d{1,2}(\.\d{1,2})?$` → basis points: "20" → 2000, "7.65" → 765. */
export function parsePercent(text: string): number | null {
  return parseHundredths(text, PERCENT_RE);
}

/** Hours text `^\d{1,3}(\.\d{1,2})?$` → hundredths: "80.5" → 8050. */
export function parseHours(text: string): number | null {
  return parseHundredths(text, HOURS_RE);
}

/** Cents → the money input's text: 123456 → "1234.56" (no grouping, so it parses back). */
export function centsToInput(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "";
  return `${Math.trunc(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
}

/** Basis points → the percent input's text: 2000 → "20.00". */
export function bpsToInput(bps: number): string {
  return centsToInput(bps);
}

/** Hundredths → the hours input's text: 8050 → "80.50"; null → "". */
export function hoursToInput(hundredths: number | null | undefined): string {
  if (hundredths === null || hundredths === undefined) return "";
  return formatHours(hundredths);
}
