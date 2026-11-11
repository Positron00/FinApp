import { applyEnv, IDENTITY, PAYROLL, TEST_ENV } from "../helpers/env";

/** PAY-NFR-09 (web LLD §7.4): the OTel wiring `startNode()` hands to @vercel/otel. */
const registered = vi.hoisted(() => ({ options: [] as Record<string, unknown>[] }));
vi.mock("@vercel/otel", () => ({
  registerOTel: (o: Record<string, unknown>) => registered.options.push(o),
}));

beforeEach(() => {
  registered.options.length = 0;
});

async function start(extra: Record<string, string> = {}) {
  applyEnv({ ...TEST_ENV, ...extra });
  const { resetConfigForTests } = await import("@/lib/config");
  resetConfigForTests();
  const { startNode } = await import("@/lib/otel");
  await startNode();
  return registered.options[0] as {
    serviceName: string;
    attributes: Record<string, string>;
    instrumentationConfig: { fetch: { propagateContextUrls: RegExp[] } };
    metricReaders: unknown[];
    logRecordProcessors: unknown[];
  };
}

describe("startNode", () => {
  // PAY-NFR-09: traceparent goes to identity and payroll, built from their base URLs (so the
  // toxiproxy override still propagates), and nowhere else.
  it("propagates context to identity and payroll only", async () => {
    const o = await start();
    expect(o.serviceName).toBe("web");
    expect(o.attributes).toMatchObject({
      "service.version": "test",
      "deployment.environment.name": "dev",
    });
    const urls = o.instrumentationConfig.fetch.propagateContextUrls;
    const matches = (u: string) => urls.some((re) => re.test(u));
    expect(matches(`${IDENTITY}/v1/users/me`)).toBe(true);
    expect(matches(`${PAYROLL}/v1/pay-runs`)).toBe(true);
    expect(matches("http://evil.test/v1/pay-runs")).toBe(false);
  });

  // PAY-NFR-09: metrics and logs are exported over OTLP only when an endpoint is set.
  it("adds the OTLP metric reader and log processor only with an endpoint", async () => {
    const off = await start();
    expect(off.metricReaders).toHaveLength(0);
    expect(off.logRecordProcessors).toHaveLength(0);
    registered.options.length = 0;
    const on = await start({ OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector:4318" });
    expect(on.metricReaders).toHaveLength(1);
    expect(on.logRecordProcessors).toHaveLength(1);
  });
});
