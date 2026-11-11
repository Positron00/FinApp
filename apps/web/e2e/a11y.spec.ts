import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { apiFetch, expect, test, watchCsp } from "./fixtures";
import { approve, FINAL, ok, prepareRun, waitForRun } from "./helpers/api";
import { readSeed } from "./helpers/env";

/**
 * Accessibility (web LLD §9.2; PAY-NFR-14): axe with the wcag2a/wcag2aa tags on every main page
 * finds no serious or critical violation; the keyboard-only path to Approve.
 * PAY-NFR-14, PAY-NFR-12.
 */
async function noSeriousViolations(page: Page, what: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const bad = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join("; ")}`);
  expect(bad, `axe on ${what}`).toEqual([]);
}

/** Waits for the page's first heading, so axe sees the rendered page, not "Loading…". */
async function ready(page: Page, heading: string | RegExp): Promise<void> {
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
  await expect(page.getByText("Loading…")).toHaveCount(0);
}

test.describe("@a11y axe", () => {
  test("public pages: /login, /accept (invalid link), /reset", async ({
    browser,
    cspViolations,
  }) => {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    watchCsp(page, cspViolations);
    await page.goto("/login");
    await ready(page, "Sign in to PayRun");
    await noSeriousViolations(page, "/login");
    await page.goto("/accept");
    await expect(page.getByRole("alert").filter({ hasText: "invite link" })).toBeVisible();
    await noSeriousViolations(page, "/accept (invalid token)");
    await page.goto("/reset");
    await expect(page.getByRole("button", { name: "Send reset link" })).toBeVisible();
    await noSeriousViolations(page, "/reset");
    await ctx.close();
  });

  test("signed-in pages: dashboard, employees, settings, users, pay runs, audit", async ({
    asOwner: page,
  }) => {
    const pages: [string, string | RegExp][] = [
      ["/dashboard", "Dashboard"],
      ["/employees", "Employees"],
      ["/employees/new", "Add employee"],
      ["/settings", "Company settings"],
      ["/users", "Users"],
      ["/pay-runs", "Pay runs"],
      ["/pay-runs/new", "New pay run"],
      ["/audit", "Audit log"],
    ];
    for (const [path, heading] of pages) {
      await page.goto(path);
      await ready(page, heading);
      await noSeriousViolations(page, path);
    }
    const employee = page.getByRole("table").getByRole("link").first();
    await page.goto("/employees");
    await employee.click();
    await expect(page).toHaveURL(/\/employees\/[0-9a-f-]{36}$/);
    await expect(page.getByRole("button", { name: "Change bank details" })).toBeVisible();
    await noSeriousViolations(page, "/employees/[id]");
  });

  test("the run page: payslips tab (calculated) and payouts tab (after payment)", async ({
    asOwner,
    asApprover,
  }) => {
    await asOwner.goto(`/pay-runs/${readSeed().runs.current}`);
    await ready(asOwner, /^Pay run /);
    await expect(asOwner.getByRole("tab", { name: "Payslips" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(
      asOwner.getByRole("tabpanel", { name: "Payslips" }).getByRole("row").nth(1),
    ).toBeVisible();
    await noSeriousViolations(asOwner, "/pay-runs/[id] payslips tab");

    const run = await prepareRun(asOwner, "biweekly");
    await asApprover.goto(`/pay-runs/${run.id}`);
    ok(await approve(asApprover, run.id), "approve");
    await waitForRun(asApprover, run.id, FINAL, 60_000);
    await asApprover.reload();
    await ready(asApprover, /^Pay run /);
    const payouts = asApprover.getByRole("tabpanel", { name: "Payouts" });
    await expect(payouts.getByRole("row").nth(1)).toBeVisible();
    await payouts.getByRole("button", { name: "History" }).first().click();
    await noSeriousViolations(asApprover, "/pay-runs/[id] payouts tab");
  });

  test("keyboard only: Tab to Approve, open the confirmation, Escape returns focus", async ({
    asOwner,
    asApprover: page,
  }) => {
    const run = await prepareRun(asOwner, "biweekly");
    try {
      test.info().annotations.push({ type: "run", description: run.id });
      await page.goto(`/pay-runs/${run.id}`);
      await ready(page, /^Pay run /);
      const approveButton = page.getByRole("button", { name: "Approve", exact: true });
      await expect(approveButton).toBeVisible();
      let reached = false;
      for (let i = 0; i < 60 && !reached; i++) {
        await page.keyboard.press("Tab");
        reached = await approveButton.evaluate((el) => el === document.activeElement);
      }
      expect(reached, "Approve is reachable with Tab").toBe(true);
      await page.keyboard.press("Enter");
      const confirm = page.getByRole("button", { name: "Confirm approval" });
      await expect(confirm).toBeVisible();
      await expect(page.locator(":focus")).toHaveCount(1, { timeout: 3_000 }); // focus moved in
      await page.keyboard.press("Escape");
      await expect(confirm).toHaveCount(0);
      await expect(approveButton).toBeFocused();
    } finally {
      ok(await apiFetch(asOwner, `/api/pay-runs/${run.id}`, { method: "DELETE" }), "delete run");
    }
  });
});
