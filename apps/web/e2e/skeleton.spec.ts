import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { apiFetch, expect, test } from "./fixtures";
import { APPROVER_EMAIL, need, readSeed } from "./helpers/env";
import { waitForTraceLogs, waitForTraceServices } from "./helpers/observability";

/**
 * The walking-skeleton journey (C51; task S8): sign in as the owner, open the seeded calculated
 * run, approve it as `approver@payrun.example.com` (dual approval: the owner calculated it), wait
 * until it is `paid`, then follow the approve request's trace through Tempo and Loki.
 * PAY-104, PAY-303, PAY-400, PAY-401, PAY-413, PAY-NFR-09, PAY-NFR-14.
 */
const HOPS = ["web", "payroll", "payout-worker", "bank-stub"] as const;

/** The run's status badge next to the page heading (payout rows have badges of their own). */
const runBadge = (p: Page, text: RegExp) =>
  p
    .getByRole("heading", { level: 1, name: /^Pay run / })
    .locator("xpath=..")
    .getByText(text);

test.describe("@skeleton", () => {
  test("@skeleton owner signs in, approver approves, run is paid, every hop is traced", async ({
    page,
    asApprover,
  }) => {
    const seed = readSeed();
    const runPath = `/pay-runs/${seed.runs.current}`;

    await test.step("sign in as the owner (PAY-104)", async () => {
      await page.goto("/login");
      await page.getByLabel("Email").fill(need("SEED_OWNER_EMAIL"));
      await page.getByLabel("Password").fill(need("SEED_OWNER_PASSWORD"));
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(page).toHaveURL(/\/dashboard$/);
      const me = await apiFetch(page, "/api/auth/me");
      expect(me.status).toBe(200);
      expect((me.body as { email: string }).email).toBe(need("SEED_OWNER_EMAIL"));
    });

    await test.step("open the seeded calculated run (PAY-303)", async () => {
      await page.goto(runPath);
      await expect(page.getByRole("heading", { level: 1, name: /^Pay run / })).toBeVisible();
      await expect(runBadge(page, /^∑\s*Calculated$/)).toBeVisible();
      // Dual approval: the owner calculated this run, so another approver must approve (PAY-401).
      await expect(page.getByText("You calculated this run.", { exact: false })).toBeVisible();
    });

    await test.step("axe: no serious or critical violations on the run page (PAY-NFR-14)", async () => {
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      const bad = results.violations
        .filter((v) => v.impact === "serious" || v.impact === "critical")
        .map((v) => `${v.id} (${v.impact}): ${v.nodes.length} node(s)`);
      expect(bad).toEqual([]);
    });

    let traceId = "";
    await test.step(`approve as ${APPROVER_EMAIL} (PAY-400)`, async () => {
      await asApprover.goto(runPath);
      await expect(runBadge(asApprover, /^∑\s*Calculated$/)).toBeVisible();
      await asApprover.getByRole("button", { name: "Approve", exact: true }).click();
      const approved = asApprover.waitForResponse(
        (r) =>
          r.url().endsWith(`/api/pay-runs/${seed.runs.current}/approve`) &&
          r.request().method() === "POST",
      );
      await asApprover.getByRole("button", { name: "Confirm approval" }).click();
      const res = await approved;
      expect(res.status()).toBe(200);
      traceId = res.headers()["x-trace-id"] ?? "";
      expect(traceId).toMatch(/^[0-9a-f]{32}$/);
    });

    await test.step("the run reaches paid (PAY-413)", async () => {
      await expect
        .poll(
          async () => {
            const r = await apiFetch(asApprover, `/api/pay-runs/${seed.runs.current}`);
            return (r.body as { status?: string }).status;
          },
          { timeout: 90_000, intervals: [1000, 2000] },
        )
        .toBe("paid");
      // The page polls every 2 s while processing and shows the final badge on its own.
      await expect(runBadge(asApprover, /^✓\s*Paid$/)).toBeVisible({ timeout: 15_000 });
    });

    await test.step("trace: spans from every hop in Tempo (PAY-NFR-09)", async () => {
      const seen = await waitForTraceServices(traceId, HOPS, 30_000);
      test.info().annotations.push({
        type: "trace",
        description: `${traceId}: ${[...seen].sort().join(", ")}`,
      });
      expect([...seen].sort(), `services in trace ${traceId}`).toEqual(
        expect.arrayContaining([...HOPS]),
      );
    });

    await test.step("logs: at least one Loki line per service for the trace (PAY-NFR-09)", async () => {
      const counts = await waitForTraceLogs(traceId, HOPS, 30_000);
      test.info().annotations.push({
        type: "loki",
        description: [...counts].map(([k, v]) => `${k}=${v}`).join(", "),
      });
      for (const svc of HOPS) {
        expect(counts.get(svc) ?? 0, `Loki lines for ${svc} in trace ${traceId}`).toBeGreaterThan(
          0,
        );
      }
    });
  });
});
