/**
 * Money display with integer arithmetic only (ADR 0006): cents are split into dollars and
 * cents with integer division; no float touches an amount. Shared (server and browser).
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
