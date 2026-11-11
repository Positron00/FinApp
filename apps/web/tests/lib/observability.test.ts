import { metrics, trace } from "@opentelemetry/api";
import {
  AggregationTemporality,
  InMemoryMetricExporter,
  MeterProvider,
  PeriodicExportingMetricReader,
} from "@opentelemetry/sdk-metrics";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";
import { http, HttpResponse } from "msw";

import { GET as listRuns } from "@/app/api/pay-runs/route";
import { logger, otelSink } from "@/lib/logger";
import { metricsApi, REQUEST_DURATION_BUCKETS } from "@/lib/metrics";
import { originRegex } from "@/lib/otel-util";
import { redact, REDACTED } from "@/lib/redact";
import { requestContext } from "@/lib/request-context";

import { PAYROLL } from "../helpers/env";
import { payRun } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn } from "../helpers/request";
import { t } from "../helpers/state";

setupMsw();

describe("logger (§7.1)", () => {
  // PAY-NFR-09: inside a span a line carries trace_id, span_id, request_id and event.
  it("adds trace and request IDs", () => {
    const provider = new BasicTracerProvider();
    const span = provider.getTracer("t").startSpan("s");
    // No context manager is registered in unit tests; the mixin reads the active span.
    vi.spyOn(trace, "getActiveSpan").mockReturnValue(span);
    requestContext.run({ requestId: "req-123" }, () => logger().info({ event: "x.y" }, "hello"));
    span.end();
    const line = t.logs.lines.at(-1)!;
    expect(line).toMatchObject({
      event: "x.y",
      msg: "hello",
      level: "info",
      service: "web",
      env: "dev",
      version: "test",
      request_id: "req-123",
      trace_id: span.spanContext().traceId,
      span_id: span.spanContext().spanId,
    });
    expect(typeof line.ts).toBe("string");
  });

  // PAY-NFR-07: deep, case-insensitive redaction of every deny-list key.
  it("redacts deny-listed keys at any depth", () => {
    logger().info({
      event: "t",
      Password: "p",
      nested: {
        deeper: {
          Authorization: "Bearer x",
          items: [{ bank_account: "123", routing_number: "9" }],
        },
      },
      "set-cookie": "c",
      session_key: "k",
      sid: "s",
      idempotency_key: "i",
      email: "e@x",
      employee_name: "n",
      note: "n",
      net_cents: 1,
      annual_salary: 2,
      hourly_rate: 3,
      private_key: "pk",
      api_key: "ak",
      secret: "s",
      token: "t",
      note_len: 4,
      user_id_hash: "abc",
      password_hash: "ph",
      token_hash: "th",
      session_len: 9,
    });
    const line = t.logs.lines.at(-1)! as Record<string, unknown>;
    for (const k of [
      "Password",
      "set-cookie",
      "session_key",
      "sid",
      "idempotency_key",
      "email",
      "employee_name",
      "note",
      "net_cents",
      "annual_salary",
      "hourly_rate",
      "private_key",
      "api_key",
      "secret",
      "token",
      // C63: the allow-list is exact names, not suffixes.
      "password_hash",
      "token_hash",
      "session_len",
    ]) {
      expect(line[k]).toBe(REDACTED);
    }
    expect(line.nested).toEqual({
      deeper: {
        Authorization: REDACTED,
        items: [{ bank_account: REDACTED, routing_number: REDACTED }],
      },
    });
    expect(line.note_len).toBe(4);
    expect(line.user_id_hash).toBe("abc");
  });

  // PAY-NFR-07: past depth 6 values are masked rather than walked.
  it("caps the walk depth", () => {
    expect(redact({ a: { b: { c: { d: { e: { f: { g: 1 } } } } } } })).toEqual({
      a: { b: { c: { d: { e: { f: REDACTED } } } } },
    });
  });

  // PAY-NFR-07: a token, cookie and JWT set during a full route call never appear in output.
  it("keeps the JWT and cookie out of logs during a route call", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs`, () =>
        HttpResponse.json({ items: [payRun()], next_cursor: null }),
      ),
    );
    await listRuns(makeReq("/api/pay-runs", { cookie }), params());
    const jwt = seen[0]!.headers.get("authorization")!.slice(7);
    expect(t.logs.text()).not.toContain(jwt);
    expect(t.logs.text()).not.toContain(cookie);
    expect(t.logs.events("http.request")[0]).toMatchObject({ route: "/api/pay-runs", status: 200 });
    expect(t.logs.events("http.request")[0]!.user_id_hash).toMatch(/^[0-9a-f]{16}$/);
  });

  // PAY-NFR-09: the OTel bridge turns a JSON line into a log record without throwing.
  it("bridges lines to the OTel logs API", () => {
    expect(() =>
      otelSink.write(
        '{"level":"warn","msg":"m","ts":"2026-11-10T00:00:00Z","event":"e","obj":{"a":1}}',
      ),
    ).not.toThrow();
    expect(() => otelSink.write("not json")).not.toThrow();
  });
});

describe("metrics (§7.2)", () => {
  // PAY-NFR-04, NFR-05: bff_request_duration{route,status} once per request, seconds, buckets.
  it("records the request histogram with the route template", async () => {
    const exporter = new InMemoryMetricExporter(AggregationTemporality.CUMULATIVE);
    const reader = new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: 60_000 });
    const provider = new MeterProvider({ readers: [reader] });
    metrics.setGlobalMeterProvider(provider);
    try {
      await listRuns(makeReq("/api/pay-runs"), params());
      t.redis.fail = true;
      const { POST: login } = await import("@/app/api/auth/login/route");
      await login(
        makeReq("/api/auth/login", { method: "POST", body: { email: "a@b.co", password: "x" } }),
        params(),
      );
      await reader.forceFlush();
      const all = exporter.getMetrics().flatMap((m) => m.scopeMetrics.flatMap((s) => s.metrics));
      const hist = all.find((m) => m.descriptor.name === "bff_request_duration")!;
      // The Collector's Prometheus exporter appends the unit: bff_request_duration_seconds.
      expect(hist.descriptor.unit).toBe("s");
      const points = hist.dataPoints as {
        attributes: Record<string, string>;
        value: { count: number; buckets: { boundaries: number[] } };
      }[];
      expect(points.map((p) => p.attributes)).toEqual(
        expect.arrayContaining([
          { route: "/api/pay-runs", status: "401" },
          { route: "/api/auth/login", status: "503" },
        ]),
      );
      expect(points[0]!.value.count).toBe(1);
      expect(points[0]!.value.buckets.boundaries).toEqual(REQUEST_DURATION_BUCKETS);
      const errors = all.find((m) => m.descriptor.name === "rate_limiter_errors")!;
      expect(errors.dataPoints[0]!.attributes).toEqual({ component: "web" });
    } finally {
      await provider.shutdown();
      metrics.disable();
    }
  });
});

describe("metrics pre-initialisation (C72)", () => {
  // PAY-603: RateLimiterErrors reads rate(rate_limiter_errors_total{component="web"}); the series
  // exists at 0 from server start (startNode calls metricsApi.init()), so the first limiter error
  // is an increase rather than a new series that rate() can't see.
  it("exports rate_limiter_errors{component=web} at 0 before any error", async () => {
    const exporter = new InMemoryMetricExporter(AggregationTemporality.CUMULATIVE);
    const reader = new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: 60_000 });
    const provider = new MeterProvider({ readers: [reader] });
    metrics.setGlobalMeterProvider(provider);
    try {
      metricsApi.init();
      await reader.forceFlush();
      const all = exporter.getMetrics().flatMap((m) => m.scopeMetrics.flatMap((s) => s.metrics));
      const errors = all.find((m) => m.descriptor.name === "rate_limiter_errors")!;
      expect(errors.dataPoints.map((p) => [p.attributes, p.value])).toEqual([
        [{ component: "web" }, 0],
      ]);
    } finally {
      await provider.shutdown();
      metrics.disable();
    }
  });
});

describe("originRegex (§7.4)", () => {
  // PAY-NFR-09: propagateContextUrls is built from the base URLs themselves.
  it("anchors and escapes the origin", () => {
    const re = originRegex("http://toxiproxy:18001");
    expect(re.test("http://toxiproxy:18001/v1/pay-runs")).toBe(true);
    expect(re.test("http://toxiproxy:180011/v1")).toBe(false);
    expect(re.test("http://evil/http://toxiproxy:18001/")).toBe(false);
  });
});
