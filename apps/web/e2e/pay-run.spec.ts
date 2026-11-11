import type { Page } from "@playwright/test";

import { apiFetch, expect, test } from "./fixtures";
import {
  allPages,
  errorCode,
  getSettings,
  ok,
  parseCents,
  randomPeriodStart,
  type PayRun,
  type Payslip,
} from "./helpers/api";
import { alertWith, runBadge, statusWith } from "./helpers/ui";

/**
 * J3 pay run (web LLD §9.2): create a monthly run in the UI; calculate refuses while hourly
 * hours are missing; enter hours; calculate; every payslip row on every page has
 * gross = net + tax + retirement and Σ net = the funding total; duplicate period; a biweekly
 * run on a wrong start; delete a calculated run and the period is free again.
 * PAY-300, PAY-301, PAY-302, PAY-303, PAY-304, PAY-NFR-01.
 */

async function createInUi(page: Page, schedule: "Monthly" | "Biweekly", start: string) {
  await page.goto("/pay-runs/new");
  await page.getByLabel("Schedule").selectOption({ label: schedule });
  await page.getByRole("textbox", { name: "Period start" }).fill(start);
  await page.getByRole("button", { name: "Create pay run" }).click();
}

/** Reads every page of the payslips table: [gross, tax, retirement, net] per row, in cents. */
async function readPayslipTable(page: Page): Promise<number[][]> {
  const panel = page.getByRole("tabpanel", { name: "Payslips" });
  const pager = panel.getByRole("navigation", { name: "Pagination" });
  const rows: number[][] = [];
  for (let n = 0; n < 100; n++) {
    const body = panel.locator("tbody tr");
    await expect(body.first()).toBeVisible();
    for (const cells of await body.evaluateAll((trs) =>
      trs.map((tr) => Array.from(tr.querySelectorAll("td")).map((td) => td.textContent ?? "")),
    )) {
      rows.push(cells.slice(3, 7).map((c) => parseCents(c) ?? Number.NaN));
    }
    const next = pager.getByRole("button", { name: "Next" });
    if (!(await next.isEnabled())) return rows;
    const first = await body.first().textContent();
    await next.click();
    await expect(body.first()).not.toHaveText(first ?? "");
  }
  throw new Error("more than 100 pages of payslips");
}

/** The value next to a `<dt>` label in the run header. */
const headerValue = (page: Page, label: string) =>
  page
    .locator("dt", { hasText: new RegExp(`^${label}$`) })
    .locator("xpath=following-sibling::dd[1]");

test.describe("@journeys J3 pay run", () => {
  test("create, enter hours, calculate, review every payslip, delete (PAY-300–304)", async ({
    asOwner: page,
  }) => {
    test.setTimeout(240_000);
    const { biweekly_anchor } = await getSettings(page);
    const start = randomPeriodStart("monthly", biweekly_anchor);
    let runId = "";

    await test.step("create a monthly run (PAY-300)", async () => {
      await createInUi(page, "Monthly", start);
      await expect(page).toHaveURL(/\/pay-runs\/[0-9a-f-]{36}$/);
      runId = page.url().split("/").at(-1) as string;
      await expect(runBadge(page, "Draft")).toBeVisible();
      await expect(page.getByRole("heading", { level: 1 })).toContainText(start);
    });

    const slips = await allPages<Payslip>(page, `/api/pay-runs/${runId}/payslips`);
    const hourly = slips.filter((p) => p.pay_type === "hourly");
    expect(hourly.length).toBeGreaterThan(0); // the full seed has hourly monthly employees

    await test.step("calculate refuses while hours are missing (PAY-302 hours_missing)", async () => {
      await page.getByRole("button", { name: "Calculate", exact: true }).click();
      await expect(
        alertWith(
          page,
          `Enter hours for every hourly employee before calculating (${hourly.length} missing).`,
        ),
      ).toBeVisible();
      await expect(page.getByLabel(/^Hours for /).first()).toHaveAttribute("aria-invalid", "true");
      await expect(runBadge(page, "Draft")).toBeVisible();
    });

    await test.step("enter hours for every hourly employee (PAY-301)", async () => {
      const inputs = page.getByLabel(/^Hours for /);
      await expect(inputs).toHaveCount(hourly.length);
      const n = await inputs.count();
      for (let i = 0; i < n; i++) await inputs.nth(i).fill(String(80 + (i % 90)) + ".25");
      await page.getByRole("button", { name: "Save hours" }).click();
      await expect(statusWith(page, "Hours saved.")).toBeVisible();
    });

    await test.step("calculate: totals shown (PAY-302)", async () => {
      await page.getByRole("button", { name: "Calculate", exact: true }).click();
      await expect(runBadge(page, "Calculated")).toBeVisible();
      await expect(headerValue(page, "Funding required")).toHaveText(/^\$[\d,]+\.\d{2}$/);
      const stored = await allPages<Payslip>(page, `/api/pay-runs/${runId}/payslips`);
      expect(
        stored.filter((p) => p.pay_type === "hourly").map((p) => (p.hours_hundredths ?? 0) % 100),
      ).toEqual(hourly.map(() => 25));
    });

    await test.step("every payslip on every page: gross = net + tax + retirement; Σ net = funding (PAY-303, NFR-01)", async () => {
      const rows = await readPayslipTable(page);
      expect(rows).toHaveLength(slips.length);
      for (const [gross, tax, retirement, net] of rows) {
        expect(gross).toBe(net! + tax! + retirement!);
        expect(net).toBeGreaterThanOrEqual(0);
      }
      const sumNet = rows.reduce((s, r) => s + r[3]!, 0);
      const funding = parseCents((await headerValue(page, "Funding required").textContent()) ?? "");
      expect(funding).toBe(sumNet);
      const run = ok<PayRun>(await apiFetch(page, `/api/pay-runs/${runId}`), "run");
      expect(run.totals?.funding_required_cents).toBe(sumNet);
      expect(run.totals?.net_cents).toBe(sumNet);
    });

    await test.step("open a payslip (PAY-303)", async () => {
      const panel = page.getByRole("tabpanel", { name: "Payslips" });
      await panel.locator("tbody tr").first().getByRole("button").click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
    });

    await test.step("a duplicate period → pay_run_exists with a link to the run (PAY-300)", async () => {
      await createInUi(page, "Monthly", start);
      await expect(
        alertWith(page, "A pay run already exists for this schedule and period."),
      ).toBeVisible();
      await expect(page).toHaveURL(/\/pay-runs\/new$/);
    });

    await test.step("a biweekly run on a wrong start → invalid_period_start (PAY-300)", async () => {
      const wrong = new Date(`${biweekly_anchor}T00:00:00Z`);
      wrong.setUTCDate(wrong.getUTCDate() + 15);
      await createInUi(page, "Biweekly", wrong.toISOString().slice(0, 10));
      await expect(
        // The LLD's text names the anchor date; the BFF says "the company's biweekly anchor".
        alertWith(page, /^Biweekly runs must start on a period start date \(every 14 days from /),
      ).toBeVisible();
    });

    await test.step("delete the calculated run; the period is free again (PAY-304)", async () => {
      await page.goto(`/pay-runs/${runId}`);
      await expect(runBadge(page, "Calculated")).toBeVisible();
      await page.getByRole("button", { name: "Delete run" }).click();
      const confirm = page.getByRole("alertdialog", { name: "Delete this pay run?" });
      await confirm.getByRole("button", { name: "Delete run" }).click();
      await expect(page).toHaveURL(/\/pay-runs$/);
      const gone = await apiFetch(page, `/api/pay-runs/${runId}`);
      expect(gone.status).toBe(404);
      expect(errorCode(gone)).toBe("pay_run_not_found");
      const again = ok<PayRun>(
        await apiFetch(page, "/api/pay-runs", {
          method: "POST",
          body: { schedule: "monthly", period_start: start },
        }),
        "recreate",
      );
      expect(again.status).toBe("draft");
      ok(await apiFetch(page, `/api/pay-runs/${again.id}`, { method: "DELETE" }), "delete");
    });
  });
});
