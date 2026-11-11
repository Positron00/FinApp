import "server-only";

import { hmac16 } from "./hmac";
import { execOrThrow, redis } from "./redis";

/**
 * Sliding-window limiter from two fixed one-minute windows (web LLD §5.1, PAY-603). Plain
 * INCR/EXPIRE/GET in one MULTI, so no scripting is needed (the `web` ACL denies it).
 */
export type Scope = "ip" | "session" | "telemetry";
export type LimitResult = { ok: true } | { ok: false; retryAfterS: number };

export class RateLimiterBackendError extends Error {
  constructor(cause: unknown) {
    super("rate limiter backend error", { cause });
    this.name = "RateLimiterBackendError";
  }
}

/** The hashed subject: no raw IP or session key is stored in Redis. */
export function limiterSubject(raw: string | null): string {
  return hmac16(raw ?? "unknown");
}

export async function limit(
  scope: Scope,
  subject: string,
  perMinute: number,
): Promise<LimitResult> {
  const nowS = Date.now() / 1000;
  const w = Math.floor(nowS / 60);
  const base = `web:rl:${scope}:${subject}`;
  let curr: number;
  let prev: number;
  try {
    const [c, , p] = await execOrThrow(
      redis()
        .multi()
        .incr(`${base}:${w}`)
        .expire(`${base}:${w}`, 120)
        .get(`${base}:${w - 1}`),
    );
    curr = Number(c);
    prev = Number(p ?? 0);
  } catch (err) {
    throw new RateLimiterBackendError(err);
  }
  const elapsed = nowS % 60;
  const estimate = curr + prev * (1 - elapsed / 60);
  if (estimate > perMinute) {
    return { ok: false, retryAfterS: Math.max(1, Math.ceil(60 - elapsed)) };
  }
  return { ok: true };
}
