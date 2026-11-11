/**
 * Deep key-based redaction for logs (web LLD §7.1, PAY-NFR-07). pino's path-based `redact`
 * can't match a key at any depth, so the logger's `formatters.log` calls this instead.
 */
const DENY = [
  // the shared deny-list (pycommon)
  "password",
  "token",
  "authorization",
  "cookie",
  "account",
  "routing",
  "api_key",
  // web additions
  "set-cookie",
  "secret",
  "private_key",
  "session",
  "sid",
  "idempotency",
  "email",
  "name",
  "note",
  "cents",
  "salary",
  "rate",
];

/** Keys that carry a derived, non-sensitive value (a length or a hash) stay readable. */
const ALLOW_SUFFIXES = ["_len", "_hash"];

export const REDACTED = "[REDACTED]";
const MAX_DEPTH = 6;

export function isDeniedKey(key: string): boolean {
  const k = key.toLowerCase();
  if (ALLOW_SUFFIXES.some((s) => k.endsWith(s))) return false;
  return DENY.some((d) => k.includes(d));
}

export function redact(value: unknown, depth = 0): unknown {
  if (value === null || typeof value !== "object") return value;
  if (depth >= MAX_DEPTH) return REDACTED;
  if (Array.isArray(value)) return value.map((v) => redact(v, depth + 1));
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value)) {
    out[k] = isDeniedKey(k) ? REDACTED : redact(v, depth + 1);
  }
  return out;
}
