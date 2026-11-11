import { apiFetch, expect, test, watchCsp } from "./fixtures";
import { ok, prepareRun, uniqueEmail } from "./helpers/api";
import { lokiUrl, tempoUrl } from "./helpers/env";
import { latestToken } from "./helpers/mailpit";
import { logCountsByService, traceServices } from "./helpers/observability";

/**
 * Tracing and telemetry hygiene (web LLD §9.2; testing.md §6).
 * (a) J4 as one trace: the approver's approve → payroll → stream → payout-worker → bank-stub and
 *     back; Tempo has spans from all four services and Loki has log lines for the trace from
 *     web, payroll and payout-worker.
 * (b) No tokens in telemetry: after an invite is accepted through the browser, neither Loki nor
 *     Tempo (url.full / url.query) holds the invite token.
 * PAY-NFR-09, PAY-NFR-07.
 */
const HOPS = ["web", "payroll", "payout-worker", "bank-stub"] as const;
const LOGGING = ["web", "payroll", "payout-worker"] as const;

/** Lines in Loki over the last 15 minutes that contain `needle`. */
async function lokiLinesContaining(needle: string): Promise<number> {
  const end = Date.now();
  const params = new URLSearchParams({
    query: `{service_name=~".+"} |= "${needle}"`,
    start: String((end - 15 * 60_000) * 1_000_000),
    end: String((end + 60_000) * 1_000_000),
    limit: "100",
  });
  const res = await fetch(`${lokiUrl()}/loki/api/v1/query_range?${params.toString()}`);
  if (!res.ok) throw new Error(`loki: HTTP ${res.status}`);
  const body = (await res.json()) as { data: { result: { values: unknown[] }[] } };
  return body.data.result.reduce((n, s) => n + s.values.length, 0);
}

/**
 * Every log entry since `sinceMs` with its labels and structured metadata (Loki merges the
 * metadata into the returned stream labels), serialised: the log bodies are short messages and
 * the fields live in the metadata, so a line filter alone would miss them.
 */
async function lokiDump(sinceMs: number): Promise<{ text: string; lines: number }> {
  const params = new URLSearchParams({
    query: `{service_name=~".+"}`,
    start: String(sinceMs * 1_000_000),
    end: String((Date.now() + 60_000) * 1_000_000),
    limit: "5000",
  });
  const res = await fetch(`${lokiUrl()}/loki/api/v1/query_range?${params.toString()}`);
  if (!res.ok) throw new Error(`loki: HTTP ${res.status}`);
  const text = await res.text();
  const body = JSON.parse(text) as { data: { result: { values: unknown[] }[] } };
  return { text, lines: body.data.result.reduce((n, s) => n + s.values.length, 0) };
}

/** Traces matching a TraceQL query over the last 15 minutes. */
async function tempoSearch(traceql: string): Promise<number> {
  const end = Math.ceil(Date.now() / 1000) + 60;
  const params = new URLSearchParams({
    q: traceql,
    start: String(end - 16 * 60),
    end: String(end),
  });
  const res = await fetch(`${tempoUrl()}/api/search?${params.toString()}`);
  if (!res.ok) throw new Error(`tempo search: HTTP ${res.status}`);
  return ((await res.json()) as { traces?: unknown[] }).traces?.length ?? 0;
}

test.describe("@trace telemetry", () => {
  test("(a) an approve is one trace across web, payroll, payout-worker and bank-stub (PAY-NFR-09)", async ({
    asOwner,
    asApprover,
  }) => {
    const run = await prepareRun(asOwner, "biweekly");
    await asApprover.goto(`/pay-runs/${run.id}`);
    const approved = asApprover.waitForResponse(
      (r) => r.url().endsWith(`/api/pay-runs/${run.id}/approve`) && r.request().method() === "POST",
    );
    await asApprover.getByRole("button", { name: "Approve", exact: true }).click();
    await asApprover.getByRole("button", { name: "Confirm approval" }).click();
    const res = await approved;
    expect(res.status()).toBe(200);
    const traceId = res.headers()["x-trace-id"] ?? "";
    expect(traceId).toMatch(/^[0-9a-f]{32}$/);
    test.info().annotations.push({ type: "trace", description: traceId });

    await expect
      .poll(async () => [...(await traceServices(traceId))].sort(), {
        message: `Tempo spans for trace ${traceId}`,
        timeout: 45_000,
        intervals: [1_000],
      })
      .toEqual(expect.arrayContaining([...HOPS]));

    await expect
      .poll(
        async () => {
          const counts = await logCountsByService(traceId);
          return LOGGING.filter((s) => (counts.get(s) ?? 0) === 0);
        },
        { message: `Loki lines for trace ${traceId}`, timeout: 45_000, intervals: [1_000] },
      )
      .toEqual([]);
  });

  test("(b) no invite token in logs or traces after an accept (PAY-NFR-07)", async ({
    asOwner,
    browser,
    cspViolations,
  }) => {
    const email = uniqueEmail();
    ok(
      await apiFetch(asOwner, "/api/users/invite", {
        method: "POST",
        body: { email, name: "Trace Probe", roles: ["approver"] },
      }),
      "invite",
    );
    const { token, link } = await latestToken(email, "accept");

    const started = Date.now();
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    watchCsp(page, cspViolations);
    await page.goto(link);
    await page.getByLabel("New password").fill("trace-password-123456");
    await page.getByLabel("Confirm password").fill("trace-password-123456");
    const accepted = page.waitForResponse((r) => r.url().endsWith("/api/auth/accept"));
    await page.getByRole("button", { name: "Set password and sign in" }).click();
    const res = await accepted;
    expect(res.status()).toBe(200);
    await expect(page).toHaveURL(/\/dashboard$/);
    const traceId = res.headers()["x-trace-id"] ?? "";
    await ctx.close();

    // The accept's own telemetry has arrived, so an absence below means something.
    await expect
      .poll(async () => (await logCountsByService(traceId)).get("web") ?? 0, {
        message: `Loki lines for the accept trace ${traceId}`,
        timeout: 45_000,
        intervals: [1_000],
      })
      .toBeGreaterThan(0);
    await expect
      .poll(async () => (await traceServices(traceId)).has("web"), {
        message: `Tempo spans for the accept trace ${traceId}`,
        timeout: 45_000,
        intervals: [1_000],
      })
      .toBe(true);

    // Loki: no line contains the token, and no label or metadata value either.
    expect(await lokiLinesContaining(token)).toBe(0);
    const dump = await lokiDump(started - 60_000);
    expect(dump.lines).toBeGreaterThan(0);
    expect(dump.lines).toBeLessThan(5000); // not truncated
    expect(dump.text.includes(token)).toBe(false);

    // Tempo: the URL attributes never carry it (web spans use http.target), and the accept's
    // whole trace doesn't contain it. The positive control proves the search itself works.
    expect(await tempoSearch(`{ span.http.target = "/api/auth/accept" }`)).toBeGreaterThan(0);
    const urlAttrs = ["url.full", "url.query", "http.target", "http.url"];
    expect(
      await tempoSearch(`{ ${urlAttrs.map((a) => `span.${a} =~ ".*${token}.*"`).join(" || ")} }`),
    ).toBe(0);
    const trace = await (await fetch(`${tempoUrl()}/api/traces/${traceId}`)).text();
    expect(trace.length).toBeGreaterThan(0);
    expect(trace.includes(token)).toBe(false);
  });
});
