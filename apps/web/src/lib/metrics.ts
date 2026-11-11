import "server-only";

import {
  metrics,
  type Counter,
  type Gauge,
  type Histogram,
  type MeterProvider,
} from "@opentelemetry/api";

/**
 * Meter `web` (web LLD §7.2). Instruments are created lazily against the current global meter
 * provider: the metrics API has no proxy meter, so instruments created before `register()`
 * would stay no-ops forever. Instrument names drop the unit and `_total` suffixes, which the
 * Collector's Prometheus exporter adds back (`bff_request_duration` + unit `s` →
 * `bff_request_duration_seconds`; a counter `bff_upstream_requests` → `..._total`).
 */

export const REQUEST_DURATION_BUCKETS = [0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2.5, 5, 10];

interface Instruments {
  requestDuration: Histogram;
  upstreamRequests: Counter;
  breakerState: Gauge;
  rateLimited: Counter;
  rateLimiterErrors: Counter;
  sessionBackendErrors: Counter;
  sessionsCreated: Counter;
  sessionsRevoked: Counter;
}

let cache: { provider: MeterProvider; inst: Instruments } | undefined;

function inst(): Instruments {
  const provider = metrics.getMeterProvider();
  if (cache?.provider === provider) return cache.inst;
  const m = provider.getMeter("web");
  const i: Instruments = {
    requestDuration: m.createHistogram("bff_request_duration", {
      unit: "s",
      description: "BFF request duration by route template and status",
      advice: { explicitBucketBoundaries: REQUEST_DURATION_BUCKETS },
    }),
    upstreamRequests: m.createCounter("bff_upstream_requests", {
      description: "BFF calls to services by target and outcome",
    }),
    breakerState: m.createGauge("circuit_breaker_state", {
      description: "0 closed, 1 half-open, 2 open",
    }),
    rateLimited: m.createCounter("rate_limited", { description: "429s from the BFF limiter" }),
    rateLimiterErrors: m.createCounter("rate_limiter_errors", {
      description: "Limiter Redis errors",
    }),
    sessionBackendErrors: m.createCounter("bff_session_backend_errors", {
      description: "Session-store Redis errors",
    }),
    sessionsCreated: m.createCounter("bff_sessions_created", { description: "Sessions created" }),
    sessionsRevoked: m.createCounter("bff_sessions_revoked", { description: "Sessions revoked" }),
  };
  cache = { provider, inst: i };
  return i;
}

export type UpstreamOutcome =
  "ok" | "http_4xx" | "http_5xx" | "timeout" | "connect_error" | "breaker_open";

export const metricsApi = {
  requestDuration(route: string, status: number, seconds: number): void {
    inst().requestDuration.record(seconds, { route, status: String(status) });
  },
  upstreamRequest(target: string, outcome: UpstreamOutcome): void {
    inst().upstreamRequests.add(1, { target, outcome });
  },
  breakerState(target: string, state: 0 | 1 | 2): void {
    inst().breakerState.record(state, { target });
  },
  rateLimited(scope: "ip" | "session" | "telemetry"): void {
    inst().rateLimited.add(1, { scope });
  },
  rateLimiterError(): void {
    inst().rateLimiterErrors.add(1, { component: "web" });
  },
  sessionBackendError(): void {
    inst().sessionBackendErrors.add(1);
  },
  sessionCreated(via: "login" | "accept"): void {
    inst().sessionsCreated.add(1, { via });
  },
  sessionsRevoked(reason: string, count: number): void {
    inst().sessionsRevoked.add(count, { reason });
  },
};
