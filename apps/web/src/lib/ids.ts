/** IDs. No `server-only` and no node imports: proxy.ts uses it. */

/** A UUIDv7 (RFC 9562): 48-bit ms timestamp, version 7, variant 10, random rest. */
export function uuidv7(now: number = Date.now()): string {
  const b = crypto.getRandomValues(new Uint8Array(16));
  let ts = now;
  for (let i = 5; i >= 0; i--) {
    b[i] = ts % 256;
    ts = Math.floor(ts / 256);
  }
  b[6] = ((b[6] ?? 0) & 0x0f) | 0x70;
  b[8] = ((b[8] ?? 0) & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/** The format proxy.ts accepts for an incoming `x-request-id` (web LLD §5.5). */
export const REQUEST_ID_RE = /^[0-9a-f-]{36}$/;
