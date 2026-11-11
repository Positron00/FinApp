import "server-only";

import { config } from "../config";
import { BffError } from "../errors";
import { logger } from "../logger";
import { metricsApi, type UpstreamOutcome } from "../metrics";
import { breakerFor } from "./breaker";

/**
 * The fetch every service client uses (web LLD §3.4.2, plan §4): a per-attempt timeout, GET-only
 * retries on 429/502/503/504 and connection failures with full-jitter backoff and a deadline, a
 * breaker per target, and per-attempt metrics. POST, PUT, PATCH and DELETE are never retried.
 *
 * Node's built-in fetch has no separate connect timeout without an undici dispatcher (and
 * undici 8 doesn't load on Node 23), so the per-attempt timeout bounds connect and read together.
 */
export interface ResilientOptions {
  readTimeoutMs?: number;
  fetchImpl?: (req: Request, init?: RequestInit) => Promise<Response>;
  sleep?: (ms: number) => Promise<void>;
  random?: () => number;
}

const RETRY_STATUSES = new Set([429, 502, 503, 504]);
const BREAKER_STATUSES = new Set([502, 503, 504]);

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** The path with UUID segments replaced, for logs (no IDs in log fields that don't need them). */
export function operationOf(req: Request): string {
  const path = new URL(req.url).pathname.replace(
    /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi,
    "{id}",
  );
  return `${req.method} ${path}`;
}

function outcomeOf(status: number): UpstreamOutcome {
  if (status >= 500) return "http_5xx";
  if (status >= 400) return "http_4xx";
  return "ok";
}

function unavailable(): BffError {
  return new BffError(503, "service_unavailable");
}

/**
 * Runs `fn` with a signal that aborts on the caller's abort or after `ms` (a TimeoutError).
 * An explicit timer rather than AbortSignal.any/timeout: the timer holds the controller
 * strongly, so a pending attempt can't be garbage-collected with its signal.
 */
async function withTimeout<T>(
  outer: AbortSignal,
  ms: number,
  fn: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const ac = new AbortController();
  const timer = setTimeout(
    () => ac.abort(new DOMException("upstream timeout", "TimeoutError")),
    ms,
  );
  const onAbort = () => ac.abort(outer.reason);
  if (outer.aborted) onAbort();
  else outer.addEventListener("abort", onAbort, { once: true });
  try {
    return await fn(ac.signal);
  } finally {
    clearTimeout(timer);
    outer.removeEventListener("abort", onAbort);
  }
}

export function resilientFetch(target: "identity" | "payroll", opts: ResilientOptions = {}) {
  return async function fetchWithPolicy(req: Request): Promise<Response> {
    const c = config();
    const readTimeoutMs = opts.readTimeoutMs ?? c.UPSTREAM_READ_TIMEOUT_MS;
    const doFetch = opts.fetchImpl ?? ((r: Request, i?: RequestInit) => fetch(r, i));
    const sleep = opts.sleep ?? defaultSleep;
    const random = opts.random ?? Math.random;
    const isGet = req.method === "GET" || req.method === "HEAD";
    const maxAttempts = isGet ? c.UPSTREAM_GET_RETRIES + 1 : 1;
    const breaker = breakerFor(target);
    const operation = operationOf(req);
    const started = Date.now();

    const fail = (kind: string, attempts: number, status?: number): void => {
      logger().warn(
        { event: "bff.upstream_error", target, operation, error_kind: kind, status, attempts },
        "upstream call failed",
      );
    };

    for (let attempt = 1; ; attempt++) {
      if (breaker.acquire() === "reject") {
        metricsApi.upstreamRequest(target, "breaker_open");
        fail("breaker_open", attempt);
        throw unavailable();
      }
      let res: Response;
      try {
        res = await withTimeout(req.signal, readTimeoutMs, (signal) =>
          doFetch(isGet ? req.clone() : req, { signal }),
        );
      } catch (err) {
        const name = (err as { name?: string } | null)?.name;
        if (name === "TimeoutError") {
          breaker.onFailure();
          metricsApi.upstreamRequest(target, "timeout");
          fail("timeout", attempt);
          throw unavailable(); // a read timeout isn't retried
        }
        if (name === "AbortError" && req.signal.aborted) {
          breaker.release(); // the caller went away: no verdict, but free a half-open probe
          throw err;
        }
        breaker.onFailure();
        metricsApi.upstreamRequest(target, "connect_error");
        if (isGet && attempt < maxAttempts && (await backoff(attempt, undefined))) continue;
        fail("connect", attempt);
        throw unavailable();
      }

      metricsApi.upstreamRequest(target, outcomeOf(res.status));
      if (BREAKER_STATUSES.has(res.status)) breaker.onFailure();
      else breaker.onSuccess();

      if (isGet && RETRY_STATUSES.has(res.status) && attempt < maxAttempts) {
        const ra = res.headers.get("retry-after");
        if (await backoff(attempt, ra)) {
          // Not awaited: cancelling one branch of a tee'd body (a cloned response, e.g. by
          // instrumentation) only settles once every branch is cancelled.
          void res.body?.cancel().catch(() => undefined);
          continue;
        }
      }
      if (res.status >= 500 || res.status === 429) fail("http", attempt, res.status);
      return res;
    }

    /** Waits before the next attempt; false when no retry may start (deadline, long Retry-After). */
    async function backoff(
      attempt: number,
      retryAfter: string | null | undefined,
    ): Promise<boolean> {
      let delay = random() * Math.min(1000, 100 * 2 ** (attempt - 1));
      if (retryAfter != null) {
        const s = Number(retryAfter);
        if (!Number.isFinite(s) || s > 1) return false; // a longer Retry-After isn't retried
        delay = Math.max(delay, s * 1000);
      }
      if (Date.now() + delay - started > c.UPSTREAM_GET_DEADLINE_MS) return false;
      logger().warn(
        { event: "bff.upstream_retry", target, operation, attempt, delay_ms: Math.round(delay) },
        "retrying upstream GET",
      );
      await sleep(delay);
      return true;
    }
  };
}
