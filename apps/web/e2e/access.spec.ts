import { randomUUID } from "node:crypto";

import { apiFetch, expect, test, watchCsp } from "./fixtures";
import { createUser, me, uniqueEmail } from "./helpers/api";
import { baseUrl, need } from "./helpers/env";
import { latestToken } from "./helpers/mailpit";
import { alertWith } from "./helpers/ui";

/**
 * J1 access (web LLD §9.2): invite → Mailpit → accept → dashboard; wrong password; used and
 * invalid invite links; password reset (old sessions end); sign out; unauthenticated redirects;
 * the session cookie's flags.
 * PAY-101, PAY-103, PAY-104, PAY-105, PAY-106, PAY-NFR-07.
 */
const INVITE_INVALID =
  "This invite link has expired or was already used. Ask your admin to send a new one.";
const WRONG_PASSWORD = "Email or password is incorrect.";

test.describe("@journeys J1 access", () => {
  test("invite, accept from the Mailpit link, land on the dashboard with exactly the invited roles (PAY-101, PAY-103, PAY-106)", async ({
    asOwner,
    browser,
    cspViolations,
  }) => {
    const email = uniqueEmail();
    const name = `J1 ${randomUUID().slice(0, 6)}`;

    await test.step("the admin invites a payroll admin from /users (PAY-101)", async () => {
      await asOwner.goto("/users");
      await asOwner.getByRole("button", { name: "Invite user" }).click();
      const dialog = asOwner.getByRole("dialog", { name: "Invite user" });
      await dialog.getByLabel("Email").fill(email);
      await dialog.getByLabel("Name").fill(name);
      await dialog.getByRole("checkbox", { name: "Payroll admin" }).check();
      await dialog.getByRole("button", { name: "Send invite" }).click();
      await expect(asOwner.getByRole("status")).toHaveText(`Invite sent to ${email}.`);
    });

    const { link } = await latestToken(email, "accept");
    expect(link.startsWith(`${baseUrl()}/accept#token=`)).toBe(true);

    const context = await browser.newContext();
    const page = await context.newPage();
    watchCsp(page, cspViolations);
    const requested: string[] = [];
    page.on("request", (r) => requested.push(r.url()));

    await test.step("open the link: the token leaves the address bar (PAY-103, NFR-07)", async () => {
      await page.goto(link);
      await expect.poll(() => page.url()).toBe(`${baseUrl()}/accept`);
      await page.getByLabel("New password").fill("j1-password-123456");
      await page.getByLabel("Confirm password").fill("j1-password-123456");
      await page.getByRole("button", { name: "Set password and sign in" }).click();
      await expect(page).toHaveURL(/\/dashboard$/);
      await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
      // The token never travels in a query string (only the POST body carries it).
      expect(requested.filter((u) => new URL(u).search.includes("token"))).toEqual([]);
    });

    await test.step("roles are exactly [payroll_admin]; admin navigation is hidden (PAY-106)", async () => {
      expect((await me(page)).roles).toEqual(["payroll_admin"]);
      const nav = page.getByRole("navigation", { name: "Main" });
      await expect(nav.getByRole("link", { name: "Employees" })).toBeVisible();
      await expect(nav.getByRole("link", { name: "Users" })).toHaveCount(0);
      await expect(nav.getByRole("link", { name: "Audit" })).toHaveCount(0);
    });

    await test.step("the session cookie is HttpOnly, SameSite=Lax and Secure (PAY-106)", async () => {
      const cookieName = process.env.SESSION_COOKIE_NAME || "sid";
      const sid = (await context.cookies()).find((c) => c.name === cookieName);
      expect(sid).toBeDefined();
      expect(sid?.httpOnly).toBe(true);
      expect(sid?.sameSite).toBe("Lax");
      expect(sid?.secure).toBe(true);
    });

    await test.step("sign out: back to /login, and Back shows no data (PAY-106)", async () => {
      await page.getByRole("button", { name: "Sign out" }).click();
      await expect(page).toHaveURL(/\/login$/);
      await page.goBack();
      await expect(page).toHaveURL(/\/login\?next=%2Fdashboard$/);
      await expect(page.getByRole("heading", { name: "Dashboard" })).toHaveCount(0);
      expect((await apiFetch(page, "/api/auth/me")).status).toBe(401);
    });

    await test.step("the used link now shows the invite_invalid message (PAY-103)", async () => {
      await page.goto(link);
      await page.getByLabel("New password").fill("j1-password-654321");
      await page.getByLabel("Confirm password").fill("j1-password-654321");
      await page.getByRole("button", { name: "Set password and sign in" }).click();
      await expect(alertWith(page, INVITE_INVALID)).toBeVisible();
    });

    await test.step("a malformed link shows the same message with no form (PAY-103)", async () => {
      await page.goto("/accept#token=bad");
      await expect(alertWith(page, INVITE_INVALID)).toBeVisible();
      await expect(page.getByLabel("New password")).toHaveCount(0);
    });

    await test.step("wrong password: one generic message (PAY-104)", async () => {
      await page.goto("/login");
      await page.getByLabel("Email").fill(email);
      await page.getByLabel("Password").fill("not-the-password-1");
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(alertWith(page, WRONG_PASSWORD)).toBeVisible();
      await expect(page).toHaveURL(/\/login$/);
    });
    await context.close();
  });

  test("password reset: Mailpit link, new password, old sessions end (PAY-105)", async ({
    asOwner,
    browser,
    cspViolations,
  }) => {
    const user = await createUser(asOwner, ["approver"]);
    const newPassword = `reset-${randomUUID()}`;

    // Session 1, signed in with the old password before the reset.
    const old = await browser.newContext();
    const oldPage = await old.newPage();
    watchCsp(oldPage, cspViolations);
    await oldPage.goto("/login");
    await oldPage.getByLabel("Email").fill(user.email);
    await oldPage.getByLabel("Password").fill(user.password);
    await oldPage.getByRole("button", { name: "Sign in" }).click();
    await expect(oldPage).toHaveURL(/\/dashboard$/);

    const context = await browser.newContext();
    const page = await context.newPage();
    watchCsp(page, cspViolations);
    const requested = new Date();

    await test.step("request a reset: always the same notice", async () => {
      await page.goto("/login");
      await page.getByRole("link", { name: "Forgot password?" }).click();
      await expect(page).toHaveURL(/\/reset$/);
      await page.getByLabel("Email").fill(user.email);
      await page.getByRole("button", { name: "Send reset link" }).click();
      await expect(page.getByRole("status")).toHaveText(
        "If an account exists for that email, we've sent a reset link.",
      );
    });

    await test.step("open the Mailpit link and set a new password", async () => {
      const { link } = await latestToken(user.email, "reset", requested);
      // As from an email client: a new tab (a same-document #fragment change would not reload).
      await page.goto("about:blank");
      await page.goto(link);
      await expect.poll(() => page.url()).toBe(`${baseUrl()}/reset`);
      await page.getByLabel("New password").fill(newPassword);
      await page.getByLabel("Confirm password").fill(newPassword);
      await page.getByRole("button", { name: "Change password" }).click();
      await expect(page).toHaveURL(/\/login\?reset=1$/);
      await expect(page.getByRole("status")).toHaveText(
        "Password changed. Sign in with your new password.",
      );
    });

    await test.step("the old session is gone (R19)", async () => {
      expect((await apiFetch(oldPage, "/api/auth/me")).status).toBe(401);
    });

    await test.step("the old password fails; the new one signs in", async () => {
      await page.getByLabel("Email").fill(user.email);
      await page.getByLabel("Password").fill(user.password);
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(alertWith(page, WRONG_PASSWORD)).toBeVisible();
      await page.getByLabel("Password").fill(newPassword);
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(page).toHaveURL(/\/dashboard$/);
      expect((await me(page)).email).toBe(user.email);
    });
    await old.close();
    await context.close();
  });

  test("unauthenticated: pages redirect to /login?next=…, /api answers 401 (PAY-106)", async ({
    page,
  }) => {
    await page.goto("/employees");
    await expect(page).toHaveURL(/\/login\?next=%2Femployees$/);
    const res = await page.request.get("/api/employees");
    expect(res.status()).toBe(401);
    expect(((await res.json()) as { error: { code: string } }).error.code).toBe("unauthenticated");

    // After signing in, `next` brings the user back (safeNext; C63).
    await page.getByLabel("Email").fill(need("SEED_OWNER_EMAIL"));
    await page.getByLabel("Password").fill(need("SEED_OWNER_PASSWORD"));
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/employees$/);
    await expect(page.getByRole("heading", { level: 1, name: "Employees" })).toBeVisible();
  });
});
