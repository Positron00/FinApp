import "server-only";

import { randomBytes } from "node:crypto";

import { z } from "zod";

import { config } from "./config";
import { bffError } from "./errors";
import { hmacHex, userIdHash } from "./hmac";
import { logger } from "./logger";
import { metricsApi } from "./metrics";
import { execOrThrow, redis } from "./redis";
import { ROLES, type Role } from "./roles";

/**
 * The session store (web LLD §2.1, §2.2, §4; PAY-106, PAY-107). Redis holds only
 * HMAC(SESSION_SECRET, cookie id), so a dump or MONITOR output has no usable cookie (R23).
 */

export interface SessionRecord {
  user_id: string;
  cid: string;
  name: string;
  roles: Role[];
  roles_version: number;
  created_at: number;
  last_seen: number;
  roles_checked_at: number;
}

export interface Session extends SessionRecord {
  key: string; // <k>, the HMAC of the cookie value
  sid: string; // key.slice(0, 32), the JWT sid claim
}

export type RevokeReason =
  | "role_change"
  | "deactivated"
  | "password_reset"
  | "roles_version_mismatch"
  | "inactive"
  | "logout";

const RoleEnum = z.enum(ROLES);
const EpochMs = z.coerce.number().int().nonnegative();

/** The Redis hash, parsed: every field is a string in Redis. */
const SessionRecordSchema = z.object({
  user_id: z.uuid(),
  cid: z.uuid(),
  name: z.string().max(200),
  roles: z
    .string()
    .transform((s, ctx) => {
      try {
        return JSON.parse(s) as unknown;
      } catch {
        ctx.addIssue({ code: "custom", message: "roles" });
        return z.NEVER;
      }
    })
    .pipe(z.array(RoleEnum)),
  roles_version: z.coerce.number().int(),
  created_at: EpochMs,
  last_seen: EpochMs,
  roles_checked_at: EpochMs,
});

/** Session data from identity's `SessionUser` (C6). */
export interface NewSession {
  user_id: string;
  company_id: string;
  name: string;
  roles: Role[];
  roles_version: number;
}

export const sessKey = (k: string) => `web:sess:${k}`;
export const userSessKey = (userId: string) => `web:user-sess:${userId}`;

/** <k> = HMAC-SHA256(SESSION_SECRET, cookie id), 64 hex chars. */
export function sessionKeyOf(cookieValue: string): string {
  return hmacHex(cookieValue);
}

function backendError(op: string, err: unknown): never {
  metricsApi.sessionBackendError();
  logger().error({ event: "session.backend_error", op, err: String(err) }, "session store error");
  throw bffError("service_unavailable");
}

/**
 * Creates a session (§4.1). With `oldCookie`, the previous session is deleted first (rotation:
 * a session ID is never reused across a sign-in). Returns the new cookie value.
 */
export async function createSession(
  s: NewSession,
  via: "login" | "accept",
  oldCookie?: string,
): Promise<string> {
  const c = config();
  const r = redis();
  const now = Date.now();
  try {
    if (oldCookie) await deleteSessionByKey(sessionKeyOf(oldCookie));
    const id = randomBytes(32).toString("base64url");
    const k = sessionKeyOf(id);
    await execOrThrow(
      r
        .multi()
        .hset(sessKey(k), {
          user_id: s.user_id,
          cid: s.company_id,
          name: s.name,
          roles: JSON.stringify(s.roles),
          roles_version: s.roles_version,
          created_at: now,
          last_seen: now,
          roles_checked_at: now,
        })
        .expire(sessKey(k), c.SESSION_IDLE_TTL_S)
        .sadd(userSessKey(s.user_id), k)
        .expire(userSessKey(s.user_id), c.SESSION_ABS_TTL_S),
    );
    await pruneIndex(s.user_id, k);
    metricsApi.sessionCreated(via);
    logger().info(
      { event: "auth.session.created", user_id_hash: userIdHash(s.user_id), via },
      "session created",
    );
    return id;
  } catch (err) {
    return backendError("create", err);
  }
}

/** Removes up to 20 index members whose session has expired (§4.2). Best effort. */
async function pruneIndex(userId: string, keep: string): Promise<void> {
  try {
    const r = redis();
    const members = (await r.smembers(userSessKey(userId))).filter((m) => m !== keep).slice(0, 20);
    const stale: string[] = [];
    for (const m of members) if ((await r.exists(sessKey(m))) === 0) stale.push(m);
    if (stale.length > 0) await r.srem(userSessKey(userId), ...stale);
  } catch {
    // The next creation retries; stale members are harmless (revocation tolerates them).
  }
}

/**
 * Loads a session by its key. Returns null when it's missing, corrupt (deleted) or past its
 * absolute TTL (deleted). Throws 503 on a Redis error: never "no session", never "allow".
 */
export async function loadSession(k: string): Promise<Session | null> {
  const c = config();
  let raw: Record<string, string>;
  try {
    raw = await redis().hgetall(sessKey(k));
  } catch (err) {
    return backendError("load", err);
  }
  if (Object.keys(raw).length === 0) return null;
  const parsed = SessionRecordSchema.safeParse(raw);
  if (!parsed.success) {
    await deleteSessionByKey(k).catch(() => undefined);
    return null;
  }
  const rec = parsed.data;
  if (Date.now() - rec.created_at > c.SESSION_ABS_TTL_S * 1000) {
    await deleteSessionByKey(k).catch(() => undefined);
    return null;
  }
  return { ...rec, key: k, sid: k.slice(0, 32) };
}

/**
 * Refreshes the idle TTL when the last touch is at least SESSION_TOUCH_INTERVAL_S old (§4.2).
 * The TTL never runs past created_at + SESSION_ABS_TTL_S. Errors are logged, never thrown.
 */
export async function touchSession(s: Session): Promise<void> {
  const c = config();
  const now = Date.now();
  if (now - s.last_seen < c.SESSION_TOUCH_INTERVAL_S * 1000) return;
  const ttl = Math.min(
    c.SESSION_IDLE_TTL_S * 1000,
    s.created_at + c.SESSION_ABS_TTL_S * 1000 - now,
  );
  try {
    await execOrThrow(
      redis()
        .multi()
        .hset(sessKey(s.key), { last_seen: now })
        .pexpire(sessKey(s.key), Math.max(ttl, 1)),
    );
    s.last_seen = now;
  } catch (err) {
    logger().warn(
      { event: "session.backend_error", op: "touch", err: String(err) },
      "touch failed",
    );
  }
}

/** Records a successful re-check: `roles_checked_at`, `roles` and `name` (C10). */
export async function markRolesChecked(s: Session, roles: Role[], name: string): Promise<void> {
  const now = Date.now();
  try {
    await redis().hset(sessKey(s.key), {
      roles_checked_at: now,
      roles: JSON.stringify(roles),
      name,
    });
  } catch (err) {
    backendError("recheck", err);
  }
  s.roles_checked_at = now;
  s.roles = roles;
  s.name = name;
}

/** Deletes one session and its index entry (sign-out). */
export async function deleteSessionByKey(k: string): Promise<void> {
  const r = redis();
  const userId = await r.hget(sessKey(k), "user_id");
  const m = r.multi().del(sessKey(k));
  if (userId) m.srem(userSessKey(userId), k);
  await execOrThrow(m);
}

/** Deletes every session of a user, then the index (§4.4). Returns the number deleted. */
export async function revokeUserSessions(userId: string, reason: RevokeReason): Promise<number> {
  const r = redis();
  try {
    const members = await r.smembers(userSessKey(userId));
    const m = r.multi();
    for (const k of members) m.del(sessKey(k));
    m.del(userSessKey(userId));
    await execOrThrow(m);
    metricsApi.sessionsRevoked(reason, members.length);
    logger().info(
      {
        event: "auth.session.revoked",
        user_id_hash: userIdHash(userId),
        reason,
        count: members.length,
      },
      "sessions revoked",
    );
    return members.length;
  } catch (err) {
    logger().error(
      {
        event: "auth.session.revoke_failed",
        user_id_hash: userIdHash(userId),
        reason,
        err: String(err),
      },
      "session revoke failed",
    );
    throw bffError("service_unavailable");
  }
}

/** The Set-Cookie attributes (§2.2). */
export function cookieOptions(maxAge?: number) {
  return {
    httpOnly: true,
    secure: true,
    sameSite: "lax" as const,
    path: "/",
    maxAge: maxAge ?? config().SESSION_ABS_TTL_S,
  };
}
