import { randomUUID } from "node:crypto";

import { apiFetch, expect, test, watchCsp } from "./fixtures";
import { approve, createUser, errorCode, ok, prepareRun, signedInPage } from "./helpers/api";
import { baseUrl, identityDirectUrl, readSeed } from "./helpers/env";
import { mintToken } from "./helpers/mint-token";
import { findRow, rowWith, statusWith } from "./helpers/ui";

/**
 * Role changes and deactivation (web LLD §9.2).
 * (a) The admin removes `approver` from B in /users: B's sessions end at once (`web:user-sess`),
 *     so B's next approve is a 401 and B lands on /login; signed in again, B has no Approve
 *     button and an approve call is 403 `forbidden_role`.
 * (b) The re-check backstop (C45): C is demoted at T by calling identity directly (bypassing the
 *     BFF's revocation); C's requests may succeed before T + 60 s, and every request C sends at
 *     or after T + 60 s gets 401 (SESSION_ROLE_RECHECK_S = 60, PAY-107).
 * (c) The admin deactivates D; /users shows D inactive with no Reactivate control (R37, C25).
 * PAY-107, PAY-108.
 */
const RECHECK_S = Number(process.env.SESSION_ROLE_RECHECK_S ?? "60");

test.describe("@journeys role changes", () => {
  test("(a) a demoted approver is signed out at once and can't approve after signing in (PAY-107, PAY-108)", async ({
    asOwner,
    browser,
    cspViolations,
  }) => {
    const b = await createUser(asOwner, ["approver"]);
    const run = await prepareRun(asOwner, "biweekly");
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    watchCsp(page, cspViolations);
    await page.goto("/login");
    await page.getByLabel("Email").fill(b.email);
    await page.getByLabel("Password").fill(b.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/dashboard$/);
    await page.goto(`/pay-runs/${run.id}`);
    await expect(page.getByRole("button", { name: "Approve", exact: true })).toBeVisible();

    await test.step("the admin changes B's roles to payroll admin in /users", async () => {
      await asOwner.goto("/users");
      const row = await findRow(asOwner, b.email);
      await row.getByRole("button", { name: `Edit roles for ${b.name}` }).click();
      const dialog = asOwner.getByRole("dialog", { name: `Edit roles for ${b.name}` });
      await dialog.getByRole("checkbox", { name: "Approver" }).uncheck();
      await dialog.getByRole("checkbox", { name: "Payroll admin" }).check();
      await dialog.getByRole("button", { name: "Save roles" }).click();
      await expect(statusWith(asOwner, `Roles updated for ${b.name}.`)).toBeVisible();
      await expect(
        rowWith(asOwner, b.email).getByRole("cell", { name: "Payroll admin" }),
      ).toBeVisible();
    });

    await test.step("B's next approve is 401 and B lands on /login", async () => {
      await page.getByRole("button", { name: "Approve", exact: true }).click();
      const res = page.waitForResponse((r) => r.url().endsWith(`/api/pay-runs/${run.id}/approve`));
      await page.getByRole("button", { name: "Confirm approval" }).click();
      expect((await res).status()).toBe(401);
      await expect(page).toHaveURL(/\/login\?next=/);
    });

    await test.step("signed in again: no Approve button; approve → 403 forbidden_role", async () => {
      await page.getByLabel("Email").fill(b.email);
      await page.getByLabel("Password").fill(b.password);
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(page).toHaveURL(new RegExp(`/pay-runs/${run.id}$`));
      await expect(page.getByRole("heading", { level: 1, name: /^Pay run / })).toBeVisible();
      await expect(page.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
      const res = await approve(page, run.id);
      expect(res.status).toBe(403);
      expect(errorCode(res)).toBe("forbidden_role");
    });
    await ctx.close();
    ok(await apiFetch(asOwner, `/api/pay-runs/${run.id}`, { method: "DELETE" }), "delete run");
  });

  test("(b) a role change made behind the BFF ends the session within 60 s (PAY-107, C45)", async ({
    asOwner,
    browser,
    request,
    cspViolations,
  }) => {
    test.setTimeout(150_000);
    const c = await createUser(asOwner, ["payroll_admin"]);
    const { context, page } = await signedInPage(browser, c.email, c.password, cspViolations);
    await page.goto("/employees");
    expect((await apiFetch(page, "/api/employees?limit=1")).status).toBe(200);

    // Demote C directly at identity: no `web:user-sess` revocation happens.
    const owner = readSeed().users.owner;
    const token = mintToken({
      aud: "identity",
      sub: owner,
      roles: ["admin", "approver", "payroll_admin"],
      name: "Owner",
    });
    const patched = await request.patch(`${identityDirectUrl()}/v1/users/${c.id}`, {
      headers: { Authorization: `Bearer ${token}`, "X-Request-ID": randomUUID() },
      data: { roles: ["approver"] },
    });
    expect(patched.status()).toBe(200);
    const t = Date.now();

    const sent: { at: number; status: number }[] = [];
    await expect
      .poll(
        async () => {
          const at = Date.now();
          const res = await apiFetch(page, "/api/employees?limit=1");
          sent.push({ at, status: res.status });
          return res.status;
        },
        {
          message: "C's session ends within SESSION_ROLE_RECHECK_S",
          timeout: (RECHECK_S + 20) * 1000,
          intervals: [1_000],
        },
      )
      .toBe(401);
    const deadline = t + RECHECK_S * 1000;
    test.info().annotations.push({
      type: "401 after (s)",
      description: String(((sent.at(-1)?.at ?? t) - t) / 1000),
    });
    // Every request sent at or after T + 60 s got 401; earlier ones were 200 or 401.
    expect(sent.filter((s) => s.at >= deadline && s.status !== 401)).toEqual([]);
    expect(sent.every((s) => s.status === 200 || s.status === 401)).toBe(true);
    // And the 401 came no later than one poll interval after the deadline.
    expect(sent.at(-1)!.at).toBeLessThanOrEqual(deadline + 2_000);
    await page.goto("/employees");
    await expect(page).toHaveURL(/\/login\?next=%2Femployees$/);
    await context.close();
  });

  test("(c) a deactivated user is inactive with no Reactivate control (PAY-108, C25)", async ({
    asOwner,
    browser,
    cspViolations,
  }) => {
    const d = await createUser(asOwner, ["approver"]);
    const { context, page } = await signedInPage(browser, d.email, d.password, cspViolations);
    expect((await apiFetch(page, "/api/auth/me")).status).toBe(200);

    await asOwner.goto("/users");
    const row = await findRow(asOwner, d.email);
    await row.getByRole("button", { name: `Deactivate ${d.name}` }).click();
    const confirm = asOwner.getByRole("alertdialog", { name: `Deactivate ${d.name}?` });
    await confirm.getByRole("button", { name: "Deactivate" }).click();
    await expect(statusWith(asOwner, `${d.name} was deactivated.`)).toBeVisible();

    const after = rowWith(asOwner, d.email);
    await expect(after.getByRole("cell", { name: "Inactive", exact: true })).toBeVisible();
    await expect(after.getByRole("button")).toHaveCount(0); // no Reactivate, Edit or Deactivate

    // D's session ended at once, and D can't sign in again.
    expect((await apiFetch(page, "/api/auth/me")).status).toBe(401);
    const login = await page.request.post("/api/auth/login", {
      data: { email: d.email, password: d.password },
      headers: { Origin: baseUrl() },
    });
    expect(login.status()).toBe(401);
    await context.close();
  });
});
