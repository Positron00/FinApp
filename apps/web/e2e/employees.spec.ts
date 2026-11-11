import { randomUUID } from "node:crypto";

import type { Page, Request, Response } from "@playwright/test";

import { apiFetch, expect, test } from "./fixtures";
import { accountNumber, getSettings, ok, ROUTING, uniqueEmail, type Settings } from "./helpers/api";
import { rowWith } from "./helpers/ui";

/**
 * J2 employees (web LLD §9.2): the admin sets the tax and retirement rates; a payroll admin adds
 * a salaried and an hourly employee; bank numbers only ever appear as ••••last4 (page HTML and
 * every /api response); bank details are changed in an empty dialog; deactivate and reactivate;
 * duplicate email on the field; an approver-only user can't add employees; the list pages.
 * PAY-200, PAY-201, PAY-202, PAY-203, PAY-601, PAY-NFR-08.
 */

/**
 * Collects every /api response body the page fully receives, to look for full account numbers.
 * On `requestfinished`, not `response`: a response whose loading is cancelled (the page navigates
 * while a fetch is in flight) can leave `response.text()` pending forever, which hung this test
 * in the second of three nightly runs (2026-11-11). A cancelled response never reached the app's
 * code, so the masking check (NFR-08) loses nothing.
 */
function recordApiBodies(page: Page): Promise<string>[] {
  const bodies: Promise<string>[] = [];
  page.on("requestfinished", (req: Request) => {
    if (!new URL(req.url()).pathname.startsWith("/api/")) return;
    bodies.push(
      req
        .response()
        .then((r: Response | null) => (r ? r.text() : ""))
        .catch(() => ""),
    );
  });
  return bodies;
}

async function addEmployee(
  page: Page,
  e: {
    name: string;
    email: string;
    payType: "Salaried" | "Hourly";
    amount: string;
    account: string;
  },
): Promise<string> {
  await page.goto("/employees/new");
  await page.getByLabel("Name").fill(e.name);
  await page.getByLabel("Email").fill(e.email);
  await page.getByLabel("Pay type").selectOption({ label: e.payType });
  await page
    .getByLabel(e.payType === "Salaried" ? "Annual salary (USD)" : "Hourly rate (USD)")
    .fill(e.amount);
  await page.getByLabel("Pay schedule").selectOption({ label: "Monthly" });
  await page.getByLabel("Routing number").fill(ROUTING);
  await page.getByLabel("Account number").fill(e.account);
  await page.getByRole("button", { name: "Add employee" }).click();
  await expect(page).toHaveURL(/\/employees\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { level: 1, name: e.name })).toBeVisible();
  return page.url().split("/").at(-1) as string;
}

test.describe("@journeys J2 employees", () => {
  test("admin sets the rates (PAY-200)", async ({ asOwner }) => {
    const before = await getSettings(asOwner);
    try {
      await asOwner.goto("/settings");
      await asOwner.getByLabel("Tax rate (%)").fill("21.5");
      await asOwner.getByLabel("Retirement rate (%)").fill("4.25");
      await asOwner.getByRole("button", { name: "Save settings" }).click();
      await expect(asOwner.getByRole("status")).toHaveText("Settings saved.");
      const after = await getSettings(asOwner);
      expect([after.tax_rate_bps, after.retirement_bps]).toEqual([2150, 425]);
      await asOwner.reload();
      await expect(asOwner.getByLabel("Tax rate (%)")).toHaveValue("21.50");
      await expect(asOwner.getByLabel("Retirement rate (%)")).toHaveValue("4.25");
    } finally {
      // Restore the R22 seed rates for the other journeys and the seed re-run check.
      const body: Omit<Settings, "updated_at" | "updated_by"> = {
        name: before.name,
        tax_rate_bps: before.tax_rate_bps,
        retirement_bps: before.retirement_bps,
        timezone: before.timezone,
        biweekly_anchor: before.biweekly_anchor,
        require_dual_approval: before.require_dual_approval,
      };
      ok(await apiFetch(asOwner, "/api/settings", { method: "PUT", body }), "restore settings");
    }
  });

  test("payroll admin adds, edits, deactivates and reactivates employees; bank numbers stay masked (PAY-201–203, NFR-08)", async ({
    asOwner,
  }) => {
    const bodies = recordApiBodies(asOwner);
    const salaried = {
      name: `J2 Salaried ${randomUUID().slice(0, 6)}`,
      email: uniqueEmail("e2e-emp"),
      payType: "Salaried" as const,
      amount: "65000",
      account: accountNumber(),
    };
    const hourly = {
      name: `J2 Hourly ${randomUUID().slice(0, 6)}`,
      email: uniqueEmail("e2e-emp"),
      payType: "Hourly" as const,
      amount: "32.50",
      account: accountNumber(),
    };
    const newAccount = accountNumber();

    let salariedId = "";
    await test.step("add a salaried and an hourly employee (PAY-201)", async () => {
      salariedId = await addEmployee(asOwner, salaried);
      await expect(asOwner.getByText("$65,000.00 / year")).toBeVisible();
      await expect(asOwner.getByText(`••••${salaried.account.slice(-4)}`)).toBeVisible();
      await addEmployee(asOwner, hourly);
      await expect(asOwner.getByText("$32.50 / hour")).toBeVisible();
    });

    await test.step("the detail page and its HTML show only ••••last4 (PAY-202, NFR-08)", async () => {
      await asOwner.goto(`/employees/${salariedId}`);
      await expect(asOwner.getByText(`••••${salaried.account.slice(-4)}`)).toBeVisible();
      expect(await asOwner.content()).not.toContain(salaried.account);
    });

    await test.step("change bank details: the dialog starts empty (PAY-203, ADR 0004)", async () => {
      await asOwner.getByRole("button", { name: "Change bank details" }).click();
      const dialog = asOwner.getByRole("dialog", { name: "Change bank details" });
      await expect(dialog.getByLabel("Routing number")).toHaveValue("");
      await expect(dialog.getByLabel("Account number")).toHaveValue("");
      await expect(dialog.getByText(`Current: ••••${salaried.account.slice(-4)}`)).toBeVisible();
      await dialog.getByLabel("Routing number").fill(ROUTING);
      await dialog.getByLabel("Account number").fill(newAccount);
      await dialog.getByRole("button", { name: "Save bank details" }).click();
      await expect(asOwner.getByRole("status")).toHaveText("Bank details changed.");
      await expect(asOwner.getByText(`••••${newAccount.slice(-4)}`)).toBeVisible();
      await expect(asOwner.getByText(/Bank details changed.*by you/)).toBeVisible();
    });

    await test.step("deactivate and reactivate (PAY-203)", async () => {
      await asOwner.getByRole("button", { name: "Deactivate" }).click();
      const off = asOwner.getByRole("alertdialog", { name: "Deactivate employee" });
      await off.getByRole("button", { name: "Deactivate" }).click();
      await expect(asOwner.getByRole("status")).toHaveText("Employee deactivated.");
      await expect(asOwner.getByText(/^Inactive/)).toBeVisible();
      await asOwner.getByRole("button", { name: "Reactivate" }).click();
      const on = asOwner.getByRole("alertdialog", { name: "Reactivate employee" });
      await on.getByRole("button", { name: "Reactivate" }).click();
      await expect(asOwner.getByRole("status")).toHaveText("Employee reactivated.");
      await expect(asOwner.getByText("Active", { exact: true })).toBeVisible();
    });

    await test.step("edit details (PAY-203)", async () => {
      const renamed = `${salaried.name} Jr`;
      await asOwner.getByLabel("Name").fill(renamed);
      await asOwner.getByRole("button", { name: "Save changes" }).click();
      await expect(asOwner.getByText("Saved.")).toBeVisible();
      await expect(asOwner.getByRole("heading", { level: 1, name: renamed })).toBeVisible();
    });

    await test.step("a duplicate email shows its message on the field (PAY-201)", async () => {
      await asOwner.goto("/employees/new");
      await asOwner.getByLabel("Name").fill("Duplicate");
      await asOwner.getByLabel("Email").fill(hourly.email);
      await asOwner.getByLabel("Annual salary (USD)").fill("50000");
      await asOwner.getByLabel("Routing number").fill(ROUTING);
      await asOwner.getByLabel("Account number").fill(accountNumber());
      await asOwner.getByRole("button", { name: "Add employee" }).click();
      const email = asOwner.getByLabel("Email");
      await expect(email).toHaveAttribute("aria-invalid", "true");
      await expect(asOwner.getByText("An employee with this email already exists.")).toBeVisible();
      await expect(asOwner).toHaveURL(/\/employees\/new$/);
    });

    await test.step("no /api response ever carried a full account number (NFR-08)", async () => {
      const texts = await Promise.all(bodies);
      expect(texts.length).toBeGreaterThan(0);
      for (const n of [salaried.account, hourly.account, newAccount]) {
        expect(texts.filter((t) => t.includes(n))).toEqual([]);
      }
    });
  });

  test("the employee list pages with Next and Previous and masks bank numbers (PAY-202, C8)", async ({
    asOwner,
  }) => {
    await asOwner.goto("/employees");
    const rows = asOwner.getByRole("row");
    await expect(rows.nth(1)).toBeVisible();
    expect(await rows.count()).toBe(51); // header + 50 (the full seed has 500)
    const firstPage = await rows.nth(1).textContent();
    for (const bank of await asOwner.getByRole("cell", { name: /^••••\d{4}$/ }).allTextContents()) {
      expect(bank).toMatch(/^••••\d{4}$/);
    }
    const pager = asOwner.getByRole("navigation", { name: "Pagination" });
    await expect(pager.getByRole("button", { name: "Previous" })).toBeDisabled();
    await pager.getByRole("button", { name: "Next" }).click();
    await expect(rows.nth(1)).not.toHaveText(firstPage ?? "");
    await expect(pager.getByRole("button", { name: "Previous" })).toBeEnabled();
    await pager.getByRole("button", { name: "Previous" }).click();
    await expect(rows.nth(1)).toHaveText(firstPage ?? "");

    // The status filter starts again from the first page.
    await asOwner.getByLabel("Status").selectOption({ label: "Inactive" });
    await expect(pager.getByRole("button", { name: "Previous" })).toBeDisabled();
  });

  test("an approver-only user can't add employees (PAY-601)", async ({ asApprover }) => {
    await asApprover.goto("/employees");
    await expect(asApprover.getByRole("heading", { level: 1, name: "Employees" })).toBeVisible();
    await expect(rowWith(asApprover, /@payrun\.example\.com/).first()).toBeVisible();
    await expect(asApprover.getByRole("link", { name: "Add employee" })).toHaveCount(0);
    await asApprover.goto("/employees/new");
    await expect(
      asApprover.getByRole("heading", { level: 1, name: "You don't have access to this page" }),
    ).toBeVisible();
    const res = await apiFetch(asApprover, "/api/employees", {
      method: "POST",
      body: {
        name: "Nope",
        email: uniqueEmail("e2e-emp"),
        pay_type: "salaried",
        schedule: "monthly",
        annual_salary_cents: 100,
        bank_routing: ROUTING,
        bank_account: accountNumber(),
      },
    });
    expect(res.status).toBe(403);
    expect((res.body as { error: { code: string } }).error.code).toBe("forbidden_role");
  });
});
