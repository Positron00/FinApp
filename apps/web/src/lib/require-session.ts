import "server-only";

import { trace } from "@opentelemetry/api";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { identityClient } from "./api/clients";
import { config } from "./config";
import { BffError, bffError } from "./errors";
import { userIdHash } from "./hmac";
import { logger } from "./logger";
import { processSingleton } from "./process-singleton";
import { currentRequestId } from "./request-context";
import { hasAnyRole, type Role } from "./roles";
import { UserMe } from "./schemas/responses";
import {
  deleteSessionByKey,
  loadSession,
  markRolesChecked,
  revokeUserSessions,
  sessionKeyOf,
  touchSession,
  type Session,
} from "./session";

/**
 * `requireSession(roles?)` (web LLD §4.3; PAY-106, PAY-107, PAY-601, PAY-603, C37). Every route
 * handler and page calls it; proxy.ts only checks that the cookie is present.
 */

export interface CheckedSession extends Session {
  /** The identity `/me` body when this request ran the re-check (the `/api/auth/me` route uses it). */
  me?: UserMe;
}

type Recheck = { kind: "ok"; me: UserMe } | { kind: "deferred" };

const inflight = processSingleton("recheck-inflight", () => new Map<string, Promise<Recheck>>());

function unauthenticated(): BffError {
  return bffError("unauthenticated", { clearCookie: true });
}

/** Calls identity `GET /v1/users/me` with the session's user token and applies §4.3 step 5. */
async function recheck(s: Session, ip: string | null): Promise<Recheck> {
  const c = config();
  const client = identityClient({
    requestId: currentRequestId() ?? "recheck",
    ip,
    claims: () => ({ sub: s.user_id, roles: s.roles, cid: s.cid, sid: s.sid, name: s.name }),
  });
  let unavailable = false;
  let me: UserMe | undefined;
  try {
    const { data, response } = await client.GET("/v1/users/me");
    if (response.status === 401 || response.status === 404) {
      await deleteSessionByKey(s.key).catch(() => undefined); // this session only (§4.3)
      throw unauthenticated();
    }
    if (!response.ok) {
      unavailable = response.status >= 500 || response.status === 429;
      if (!unavailable) throw bffError("internal_error");
    } else {
      const parsed = UserMe.safeParse(data);
      if (!parsed.success) {
        logger().error(
          {
            event: "bff.response_invalid",
            target: "identity",
            operation: "users_me",
            issues: parsed.error.issues.map((i) => i.path.join(".")),
          },
          "invalid identity response",
        );
        throw bffError("internal_error");
      }
      me = parsed.data;
    }
  } catch (err) {
    if (err instanceof BffError && err.code === "service_unavailable") unavailable = true;
    else throw err;
  }

  if (unavailable || !me) {
    const age = Date.now() - s.roles_checked_at;
    if (age < c.SESSION_RECHECK_GRACE_S * 1000) {
      logger().warn(
        {
          event: "auth.session.recheck_deferred",
          user_id_hash: userIdHash(s.user_id),
          age_s: Math.floor(age / 1000),
        },
        "identity unavailable; keeping cached roles",
      );
      return { kind: "deferred" };
    }
    throw bffError("service_unavailable"); // C37: past the grace window
  }

  if (me.id !== s.user_id || me.company_id !== s.cid) {
    logger().error(
      { event: "bff.response_invalid", target: "identity", operation: "users_me", issues: ["id"] },
      "identity returned another user",
    );
    await deleteSessionByKey(s.key).catch(() => undefined);
    throw unauthenticated();
  }
  if (me.status !== "active" || me.roles_version !== s.roles_version) {
    const reason = me.status !== "active" ? "inactive" : "roles_version_mismatch";
    await revokeUserSessions(s.user_id, reason).catch(() => undefined);
    throw unauthenticated();
  }
  await markRolesChecked(s, me.roles, me.name);
  return { kind: "ok", me };
}

/** Single-flight per session key within the process: parallel requests share one `/me` call. */
function recheckOnce(s: Session, ip: string | null): Promise<Recheck> {
  let p = inflight.get(s.key);
  if (!p) {
    p = recheck(s, ip).finally(() => inflight.delete(s.key));
    inflight.set(s.key, p);
  }
  return p;
}

export interface ResolveOptions {
  roles?: Role[];
  ip?: string | null;
}

/**
 * The forced re-check of `GET /api/auth/me`: returns identity's profile, or undefined when the
 * re-check was deferred (identity down inside the grace window).
 */
export async function recheckNow(
  s: CheckedSession,
  ip: string | null,
): Promise<UserMe | undefined> {
  if (s.me) return s.me;
  const r = await recheckOnce(s, ip);
  return r.kind === "ok" ? r.me : undefined;
}

/** The algorithm, given the cookie value. Throws BffError (401, 403, 503). */
export async function resolveSession(
  cookieValue: string | undefined,
  opts: ResolveOptions = {},
): Promise<CheckedSession> {
  return trace.getTracer("web").startActiveSpan("bff.session.require", async (span) => {
    try {
      const c = config();
      if (!cookieValue) throw bffError("unauthenticated");
      const s: CheckedSession | null = await loadSession(sessionKeyOf(cookieValue));
      if (!s) throw unauthenticated();
      const due = Date.now() - s.roles_checked_at >= c.SESSION_ROLE_RECHECK_S * 1000;
      span.setAttribute("session.recheck", due);
      if (due) {
        const r = await recheckOnce(s, opts.ip ?? null);
        if (r.kind === "ok") s.me = r.me;
      }
      await touchSession(s);
      if (opts.roles && opts.roles.length > 0 && !hasAnyRole(s.roles, opts.roles)) {
        throw bffError("forbidden_role");
      }
      return s;
    } finally {
      span.end();
    }
  });
}

/** For route handlers: reads the cookie with `cookies()`. */
export async function requireSession(roles?: Role[]): Promise<CheckedSession> {
  const jar = await cookies();
  return resolveSession(jar.get(config().SESSION_COOKIE_NAME)?.value, { roles });
}

/**
 * For pages: redirects to `/login?next=<path>` on 401, returns "forbidden" on 403 (the page
 * renders `<Forbidden/>`), and throws on 503 (error.tsx shows "temporarily unavailable").
 */
export async function requireSessionForPage(
  path: string,
  roles?: Role[],
): Promise<CheckedSession | "forbidden"> {
  try {
    return await requireSession(roles);
  } catch (err) {
    if (err instanceof BffError && err.status === 401) {
      redirect(`/login?next=${encodeURIComponent(path)}`);
    }
    if (err instanceof BffError && err.status === 403) return "forbidden";
    throw err;
  }
}

/** Tests only. */
export function resetRecheckForTests(): void {
  inflight.clear();
}
