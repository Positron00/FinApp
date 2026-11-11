import { metrics } from "@opentelemetry/api";
import {
  AggregationTemporality,
  InMemoryMetricExporter,
  MeterProvider,
  PeriodicExportingMetricReader,
} from "@opentelemetry/sdk-metrics";

import { operationOf, resilientFetch } from "@/lib/api/resilient-fetch";

import { applyEnv, PAYROLL, TEST_ENV } from "../../helpers/env";
import { t } from "../../helpers/state";

/** plan §4: timeouts, GET-only retries, backoff, deadline and metrics. */
type Step = Response | Error | "hang";

function scripted(steps: Step[]) {
  const calls: Request[] = [];
  const impl = async (req: Request, init?: RequestInit): Promise<Response> => {
    calls.push(req);
    const s = steps[Math.min(calls.length - 1, steps.length - 1)]!;
    if (s === "hang") {
      return new Promise((_, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal!.reason));
      });
    }
    if (s instanceof Error) throw s;
    return s.clone();
  };
  return { impl, calls };
}

const noSleep = async () => undefined;
const get = () => new Request(`${PAYROLL}/v1/pay-runs/0193a000-0000-7000-8000-0000000000b1`);
const status = (n: number, headers: Record<string, string> = {}) =>
  new Response("{}", { status: n, headers });

async function useConfig(over: Record<string, string>) {
  applyEnv({ ...TEST_ENV, ...over });
  const { resetConfigForTests } = await import("@/lib/config");
  resetConfigForTests();
}

describe("resilientFetch", () => {
  // plan §4: GET is retried twice (3 attempts) on 429, 502, 503 and 504.
  it.each([429, 502, 503, 504])("retries GET on %i", async (code) => {
    const { impl, calls } = scripted([status(code), status(code), status(200)]);
    const res = await resilientFetch("payroll", { fetchImpl: impl, sleep: noSleep })(get());
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(3);
    expect(t.logs.events("bff.upstream_retry")).toHaveLength(2);
  });

  // plan §4: not retried on 500 or 4xx.
  it.each([500, 400, 404, 409])("does not retry GET on %i", async (code) => {
    const { impl, calls } = scripted([status(code), status(200)]);
    const res = await resilientFetch("payroll", { fetchImpl: impl, sleep: noSleep })(get());
    expect(res.status).toBe(code);
    expect(calls).toHaveLength(1);
  });

  // plan §4: connection failures are retried for GET; the last failure is a 503.
  it("retries GET on connect errors, then 503", async () => {
    const err = Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
    const { impl, calls } = scripted([err]);
    await expect(
      resilientFetch("identity", { fetchImpl: impl, sleep: noSleep })(get()),
    ).rejects.toMatchObject({
      status: 503,
      code: "service_unavailable",
    });
    expect(calls).toHaveLength(3);
    expect(t.logs.events("bff.upstream_error")[0]).toMatchObject({
      error_kind: "connect",
      attempts: 3,
    });
  });

  // plan §4: POST, PUT, PATCH and DELETE are never retried.
  it.each(["POST", "PUT", "PATCH", "DELETE"])("never retries %s", async (method) => {
    const { impl, calls } = scripted([status(503), status(200)]);
    const req = new Request(`${PAYROLL}/v1/x`, {
      method,
      body: method === "DELETE" ? undefined : "{}",
    });
    const res = await resilientFetch("payroll", { fetchImpl: impl, sleep: noSleep })(req);
    expect(res.status).toBe(503);
    expect(calls).toHaveLength(1);
  });

  // plan §4: a read timeout → 503, not retried.
  it("maps a read timeout to 503 without retrying", async () => {
    const { impl, calls } = scripted(["hang"]);
    await expect(
      resilientFetch("payroll", { fetchImpl: impl, sleep: noSleep, readTimeoutMs: 30 })(get()),
    ).rejects.toMatchObject({ status: 503 });
    expect(calls).toHaveLength(1);
    expect(t.logs.events("bff.upstream_error")[0]).toMatchObject({ error_kind: "timeout" });
  });

  // plan §4: the default read timeout comes from UPSTREAM_READ_TIMEOUT_MS.
  it("uses UPSTREAM_READ_TIMEOUT_MS by default", async () => {
    await useConfig({ UPSTREAM_READ_TIMEOUT_MS: "20" });
    const { impl } = scripted(["hang"]);
    await expect(
      resilientFetch("payroll", { fetchImpl: impl, sleep: noSleep })(get()),
    ).rejects.toMatchObject({ status: 503 });
  });

  // plan §4: a Retry-After ≤ 1 s is honoured; a longer one isn't retried.
  it("honours a short Retry-After and returns a long one", async () => {
    const delays: number[] = [];
    const sleep = async (ms: number) => void delays.push(ms);
    const short = scripted([status(429, { "Retry-After": "1" }), status(200)]);
    expect(
      (await resilientFetch("payroll", { fetchImpl: short.impl, sleep, random: () => 0 })(get()))
        .status,
    ).toBe(200);
    expect(delays).toEqual([1000]);
    const long = scripted([status(429, { "Retry-After": "30" }), status(200)]);
    const res = await resilientFetch("payroll", { fetchImpl: long.impl, sleep })(get());
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("30");
    expect(long.calls).toHaveLength(1);
  });

  // plan §4: full-jitter backoff random(0, min(1000, 100 × 2^n)).
  it("backs off with full jitter", async () => {
    const delays: number[] = [];
    const { impl } = scripted([status(503), status(503), status(200)]);
    await resilientFetch("payroll", {
      fetchImpl: impl,
      sleep: async (ms) => void delays.push(ms),
      random: () => 1,
    })(get());
    expect(delays).toEqual([100, 200]);
  });

  // plan §4: no retry starts after UPSTREAM_GET_DEADLINE_MS.
  it("stops retrying at the deadline", async () => {
    await useConfig({ UPSTREAM_GET_DEADLINE_MS: "50" });
    const { impl, calls } = scripted([status(503)]);
    const res = await resilientFetch("payroll", {
      fetchImpl: impl,
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      random: () => 0.9,
    })(get());
    expect(res.status).toBe(503);
    expect(calls.length).toBeLessThan(3);
  });

  // plan §4: the caller's abort is passed through, not turned into a 503.
  it("rethrows the caller's abort", async () => {
    const ac = new AbortController();
    const { impl } = scripted(["hang"]);
    const p = resilientFetch("payroll", { fetchImpl: impl, sleep: noSleep })(
      new Request(`${PAYROLL}/v1/x`, { signal: ac.signal }),
    );
    ac.abort();
    await expect(p).rejects.toMatchObject({ name: "AbortError" });
  });

  // PAY-NFR-07: operations in logs carry templates, not IDs.
  it("logs the operation with IDs replaced", () => {
    expect(operationOf(get())).toBe("GET /v1/pay-runs/{id}");
  });

  // plan §4, PAY-NFR-10: bff_upstream_requests{target, outcome} per attempt.
  it("counts each attempt by outcome", async () => {
    const exporter = new InMemoryMetricExporter(AggregationTemporality.CUMULATIVE);
    const reader = new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: 60_000 });
    const provider = new MeterProvider({ readers: [reader] });
    metrics.setGlobalMeterProvider(provider);
    try {
      const { impl } = scripted([status(503), status(404)]);
      await resilientFetch("payroll", { fetchImpl: impl, sleep: noSleep })(get());
      await reader.forceFlush();
      const points = exporter
        .getMetrics()
        .flatMap((m) => m.scopeMetrics.flatMap((s) => s.metrics))
        .filter((m) => m.descriptor.name === "bff_upstream_requests")
        .flatMap((m) => m.dataPoints as { attributes: Record<string, unknown>; value: unknown }[]);
      const byOutcome = Object.fromEntries(
        points.map((p) => [String(p.attributes.outcome), p.value]),
      );
      expect(byOutcome).toEqual({ http_5xx: 1, http_4xx: 1 });
    } finally {
      await provider.shutdown();
      metrics.disable();
    }
  });
});
