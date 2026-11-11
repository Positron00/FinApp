import "server-only";

import { SpanStatusCode, trace } from "@opentelemetry/api";
import type { IdentityClient, PayrollClient } from "@payrun/api-client";
import { NextResponse, type NextRequest } from "next/server";
import type pino from "pino";
import type { z } from "zod";

import { identityClient, payrollClient, slowPayrollFetch, type ClientContext } from "./api/clients";
import { readJson, rejectBody } from "./body";
import { clientIp } from "./client-ip";
import { config } from "./config";
import { asBffError, bffError, BffError, errorBody, zodDetails } from "./errors";
import { REQUEST_ID_RE, uuidv7 } from "./ids";
import { serviceClaims, type TokenClaims } from "./internal-jwt";
import { logger, userIdHash } from "./logger";
import { metricsApi } from "./metrics";
import { checkOrigin } from "./origin";
import { limit, limiterSubject, RateLimiterBackendError } from "./rate-limit";
import { requestContext } from "./request-context";
import { resolveSession, type CheckedSession } from "./require-session";
import type { Role } from "./roles";
import { IdempotencyKeyHeader } from "./schemas/requests";
import { loadSession, sessionKeyOf } from "./session";

/**
 * The BFF request pipeline (web LLD §3.1): context → IP limit → Origin → session → session limit
 * → params, query and headers → body → clients → handler → finish (error mapping, standard
 * headers, metric, `http.request` log). Every `/api/*` handler is built with `route()`.
 */

export type Auth = "public" | "optional" | { roles?: Role[] };
export type IpLimit = "default" | "telemetry" | "none";

export interface RouteCtx<B, Q, P> {
  req: NextRequest;
  body: B;
  query: Q;
  params: P;
  session: CheckedSession | null;
  requestId: string;
  log: pino.Logger;
  ip: string | null;
  idempotencyKey: string | null;
  identity: IdentityClient;
  payroll: PayrollClient;
  /** The calculate/approve fetch (UPSTREAM_SLOW_READ_TIMEOUT_MS) for openapi-fetch's `fetch` option. */
  slowFetch: (req: Request) => Promise<Response>;
}

export interface RouteDef<B, Q, P> {
  /** The route template, e.g. "/api/pay-runs/[id]/approve"; the metric label. */
  name: string;
  auth: Auth;
  /** "fail_closed" for login, reset and accept (PAY-603). Default "fail_open". */
  publicLimiter?: "fail_closed" | "fail_open";
  ipLimit?: IpLimit;
  body?: z.ZodType<B>;
  query?: z.ZodType<Q>;
  params?: z.ZodType<P>;
  bodyLimit?: number;
  /** C34: missing → 400 `idempotency_key_required`; not a UUID → 422. */
  idempotencyKey?: boolean;
  /** Skip the body step (the unknown-path fallback answers 404 whatever the body). */
  ignoreBody?: boolean;
  /** `http.request` at debug (health routes). */
  quiet?: boolean;
  handler(ctx: RouteCtx<B, Q, P>): Promise<Response>;
}

type ParamsArg = { params: Promise<Record<string, string | string[] | undefined>> };
export type Handler = (req: NextRequest, ctx: ParamsArg) => Promise<NextResponse>;

export function json(body: unknown, status = 200, init: ResponseInit = {}): NextResponse {
  return NextResponse.json(body, { ...init, status });
}

function currentTraceId(): string {
  return trace.getActiveSpan()?.spanContext().traceId ?? "0".repeat(32);
}

/** Sets x-request-id, x-trace-id and Cache-Control: no-store on every response (PAY-604). */
function finishHeaders(res: Response, requestId: string, traceId: string): void {
  res.headers.set("x-request-id", requestId);
  res.headers.set("x-trace-id", traceId);
  res.headers.set("Cache-Control", "no-store");
}

export function errorResponse(err: BffError, requestId: string): NextResponse {
  const res = json(errorBody(err, requestId), err.status);
  if (err.retryAfter !== undefined) res.headers.set("Retry-After", String(err.retryAfter));
  if (err.clearCookie) {
    res.cookies.set(config().SESSION_COOKIE_NAME, "", {
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
      maxAge: 0,
    });
  }
  return res;
}

function queryObject(url: URL): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of url.searchParams) out[k] = v;
  return out;
}

async function applyLimit(
  scope: "ip" | "session" | "telemetry",
  raw: string | null,
  perMinute: number,
  mode: "fail_closed" | "fail_open",
  route: string,
): Promise<void> {
  try {
    const r = await limit(scope, limiterSubject(raw), perMinute);
    if (!r.ok) {
      metricsApi.rateLimited(scope);
      logger().info({ event: "ratelimit.limited", scope, route }, "rate limited");
      throw bffError("rate_limited", { retryAfter: r.retryAfterS });
    }
  } catch (err) {
    if (!(err instanceof RateLimiterBackendError)) throw err;
    metricsApi.rateLimiterError();
    logger().warn(
      { event: "ratelimit.backend_error", scope, route, mode },
      "limiter backend error",
    );
    if (mode === "fail_closed") throw bffError("service_unavailable");
  }
}

export function route<B = undefined, Q = undefined, P = undefined>(
  def: RouteDef<B, Q, P>,
): Handler {
  return async function handle(req, routeCtx) {
    const started = performance.now();
    const incoming = req.headers.get("x-request-id");
    const requestId = incoming && REQUEST_ID_RE.test(incoming) ? incoming : uuidv7();
    const tracer = trace.getTracer("web");

    return requestContext.run({ requestId }, () =>
      tracer.startActiveSpan(`BFF ${req.method} ${def.name}`, async (span) => {
        span.setAttribute("http.route", def.name);
        span.setAttribute("http.request.method", req.method);
        const log = logger().child({ route: def.name });
        let session: CheckedSession | null = null;
        let res: NextResponse;
        let code: string | undefined;
        try {
          res = await pipeline(def, req, routeCtx, requestId, log, (s) => (session = s));
        } catch (err) {
          const e = asBffError(err);
          if (!(err instanceof BffError)) {
            log.error(
              { event: "bff.unhandled_error", exc: err instanceof Error ? err.stack : String(err) },
              "unhandled error in route",
            );
            span.setStatus({ code: SpanStatusCode.ERROR });
          }
          code = e.code;
          res = errorResponse(e, requestId);
        }
        const traceId = currentTraceId();
        finishHeaders(res, requestId, traceId);
        const seconds = (performance.now() - started) / 1000;
        metricsApi.requestDuration(def.name, res.status, seconds);
        span.setAttribute("http.response.status_code", res.status);
        const s = session as CheckedSession | null;
        const line = {
          event: "http.request",
          method: req.method,
          status: res.status,
          duration_ms: Math.round(seconds * 1000),
          ...(s ? { user_id_hash: userIdHash(s.user_id) } : {}),
          ...(code ? { code } : {}),
        };
        if (def.quiet) log.debug(line, "request");
        else log.info(line, "request");
        span.end();
        return res;
      }),
    );
  };
}

async function pipeline<B, Q, P>(
  def: RouteDef<B, Q, P>,
  req: NextRequest,
  routeCtx: ParamsArg,
  requestId: string,
  log: pino.Logger,
  setSession: (s: CheckedSession) => void,
): Promise<NextResponse> {
  const c = config();
  const ip = clientIp(req.headers, c.TRUSTED_PROXY_HOPS);

  // 2. IP rate limit
  const ipLimit = def.ipLimit ?? "default";
  if (ipLimit === "default") {
    await applyLimit("ip", ip, c.BFF_RATE_LIMIT_IP, def.publicLimiter ?? "fail_open", def.name);
  } else if (ipLimit === "telemetry") {
    await applyLimit("telemetry", ip, c.BFF_TELEMETRY_RATE_LIMIT_IP, "fail_open", def.name);
  }

  // 3. Origin, for mutations
  checkOrigin(req, def.name);

  // 4. Session
  let session: CheckedSession | null = null;
  const cookie = req.cookies.get(c.SESSION_COOKIE_NAME)?.value;
  if (typeof def.auth === "object") {
    session = await resolveSession(cookie, { roles: def.auth.roles, ip });
  } else if (def.auth === "optional" && cookie) {
    session = await loadSession(sessionKeyOf(cookie)).catch(() => null);
  }
  if (session) setSession(session);

  // 5. Session rate limit. Telemetry has its own per-IP budget and never spends the session's,
  // so beacons can't push the run page's polling into 429s (C63).
  if (session && ipLimit !== "telemetry") {
    await applyLimit("session", session.key, c.BFF_RATE_LIMIT_SESSION, "fail_open", def.name);
  }

  // 6. Params, query and headers
  const rawParams = await routeCtx.params;
  const params = parseOr422(def.params, rawParams, "path") as P;
  const query = parseOr422(def.query, queryObject(req.nextUrl), "query") as Q;
  let idempotencyKey: string | null = null;
  if (def.idempotencyKey) {
    const raw = req.headers.get("idempotency-key");
    if (raw === null || raw === "") throw bffError("idempotency_key_required");
    const k = IdempotencyKeyHeader.safeParse(raw);
    if (!k.success) {
      throw bffError("validation_error", {
        details: [{ field: "header.Idempotency-Key", issue: k.error.issues[0]?.code ?? "invalid" }],
      });
    }
    idempotencyKey = k.data;
  }

  // 7. Body
  let body = undefined as B;
  if (def.body) body = await readJson(req, def.body, def.name, def.bodyLimit);
  else if (!def.ignoreBody && req.method !== "GET" && req.method !== "HEAD") {
    await rejectBody(req, def.name);
  }

  // 8. Clients: a user token bound to the session, or service:web on public flows (R9, C10)
  const claims = (): TokenClaims =>
    session
      ? {
          sub: session.user_id,
          roles: session.roles,
          cid: session.cid,
          sid: session.sid,
          name: session.name,
        }
      : serviceClaims();
  const clientCtx: ClientContext = { requestId, ip, claims };
  let identity: IdentityClient | undefined;
  let payroll: PayrollClient | undefined;

  // 9. Handler
  return (await def.handler({
    req,
    body,
    query,
    params,
    session,
    requestId,
    log,
    ip,
    idempotencyKey,
    get identity() {
      return (identity ??= identityClient(clientCtx));
    },
    get payroll() {
      return (payroll ??= payrollClient(clientCtx));
    },
    slowFetch: slowPayrollFetch(clientCtx),
  })) as NextResponse;
}

function parseOr422(schema: z.ZodType | undefined, value: unknown, where: string): unknown {
  if (!schema) return undefined;
  const r = schema.safeParse(value);
  if (!r.success) throw bffError("validation_error", { details: zodDetails(where, r.error) });
  return r.data;
}

/**
 * The handler for a standard method a route doesn't implement: 405 `method_not_allowed` with
 * `Allow` and the standard headers; no session, limiter or body step (web LLD §1.2).
 */
export function methodNotAllowed(name: string, allowed: string[]): Handler {
  return async function notAllowed(req) {
    const incoming = req.headers.get("x-request-id");
    const requestId = incoming && REQUEST_ID_RE.test(incoming) ? incoming : uuidv7();
    const started = performance.now();
    const res = errorResponse(bffError("method_not_allowed"), requestId);
    res.headers.set("Allow", allowed.join(", "));
    finishHeaders(res, requestId, currentTraceId());
    metricsApi.requestDuration(name, 405, (performance.now() - started) / 1000);
    return res;
  };
}

/** Builds a JSON response from a parsed service body, or 500 + `bff.response_invalid`. */
export function parseUpstream<T>(
  schema: z.ZodType<T>,
  data: unknown,
  target: "identity" | "payroll",
  operation: string,
): T {
  const r = schema.safeParse(data);
  if (!r.success) {
    logger().error(
      {
        event: "bff.response_invalid",
        target,
        operation,
        issues: r.error.issues.map((i) => i.path.join(".")),
      },
      "invalid service response",
    );
    throw bffError("internal_error");
  }
  return r.data;
}
