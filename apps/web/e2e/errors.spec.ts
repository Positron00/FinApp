import { apiFetch, expect, test, watchCsp } from "./fixtures";
import { accountNumber, errorCode, ROUTING, uniqueEmail } from "./helpers/api";
import { baseUrl, need } from "./helpers/env";
import { startService, stopService } from "./helpers/stack";
import { resetProxies, setProxyEnabled } from "./helpers/toxiproxy";
import { alertWith } from "./helpers/ui";

/**
 * Errors and security controls (web LLD §9.2; plan §12.1): the 404 page; a forbidden page;
 * identity down (login says "try again" and is 503, while a signed-in user still lists
 * employees through payroll); payroll down via Toxiproxy (the error banner with a reference);
 * a foreign Origin → 403 `bad_origin`; a 70 KB body → 413 `payload_too_large`; the security
 * headers on a page and an /api route.
 * PAY-602, PAY-603, PAY-604, plan §12.1.
 */
const UNAVAILABLE = "The service is temporarily unavailable. Please try again.";

const employeeBody = (extra: Record<string, unknown> = {}) => ({
  name: "Error Probe",
  email: uniqueEmail("e2e-emp"),
  pay_type: "salaried",
  schedule: "monthly",
  annual_salary_cents: 100_000,
  bank_routing: ROUTING,
  bank_account: accountNumber(),
  ...extra,
});

test.describe("@journeys errors", () => {
  test("unknown page → the 404 page; unknown /api path → 404 not_found (PAY-602)", async ({
    asOwner,
  }) => {
    const res = await asOwner.goto("/no-such-page");
    expect(res?.status()).toBe(404);
    await expect(asOwner.getByRole("heading", { level: 1, name: "Page not found" })).toBeVisible();
    const api = await apiFetch(asOwner, "/api/no-such-route");
    expect(api.status).toBe(404);
    expect(errorCode(api)).toBe("not_found");
    expect((api.body as { error: { request_id: string } }).error.request_id).toMatch(
      /^[0-9a-f-]{36}$/,
    );
  });

  test("a page the role can't open renders Forbidden (PAY-601)", async ({ asApprover }) => {
    await asApprover.goto("/users");
    await expect(
      asApprover.getByRole("heading", { level: 1, name: "You don't have access to this page" }),
    ).toBeVisible();
    await expect(asApprover.getByRole("link", { name: "Go to the dashboard" })).toBeVisible();
    const api = await apiFetch(asApprover, "/api/users");
    expect(api.status).toBe(403);
    expect(errorCode(api)).toBe("forbidden_role");
  });

  test.describe("identity down", () => {
    test.afterAll(async ({ browser }) => {
      startService("identity");
      // The web's identity breaker half-opens after BREAKER_RESET_S: wait until sign-in works,
      // so the next journeys start on a healthy stack.
      const ctx = await browser.newContext({ baseURL: baseUrl() });
      await expect
        .poll(
          async () =>
            (
              await ctx.request.post("/api/auth/login", {
                data: { email: need("SEED_OWNER_EMAIL"), password: need("SEED_OWNER_PASSWORD") },
                headers: { Origin: baseUrl() },
              })
            ).status(),
          {
            message: "sign-in works again after identity restarts",
            timeout: 90_000,
            intervals: [1_000],
          },
        )
        .toBe(200);
      await ctx.close();
    });

    test("login says try again (503); a signed-in user still lists employees (PAY-602, C37)", async ({
      browser,
      asOwner,
      cspViolations,
    }) => {
      // Warm the owner's session first, so its role re-check is recent (C37 grace).
      await asOwner.goto("/employees");
      await expect(asOwner.getByRole("row").nth(1)).toBeVisible();
      stopService("identity");

      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      watchCsp(page, cspViolations);
      await page.goto("/login");
      await page.getByLabel("Email").fill(need("SEED_OWNER_EMAIL"));
      await page.getByLabel("Password").fill(need("SEED_OWNER_PASSWORD"));
      const login = page.waitForResponse((r) => r.url().endsWith("/api/auth/login"));
      await page.getByRole("button", { name: "Sign in" }).click();
      const res = await login;
      expect(res.status()).toBe(503);
      expect(((await res.json()) as { error: { code: string } }).error.code).toBe(
        "service_unavailable",
      );
      await expect(alertWith(page, UNAVAILABLE)).toBeVisible();
      await expect(page).toHaveURL(/\/login$/);
      await ctx.close();

      // The payroll path doesn't need identity: the signed-in owner still lists employees.
      await asOwner.reload();
      await expect(asOwner.getByRole("heading", { level: 1, name: "Employees" })).toBeVisible();
      await expect(asOwner.getByRole("row").nth(1)).toBeVisible();
      const list = await apiFetch(asOwner, "/api/employees?limit=5");
      expect(list.status).toBe(200);
    });
  });

  test.describe("payroll down", () => {
    test.afterEach(async ({ asOwner }) => {
      await resetProxies();
      // Wait out the BFF's payroll breaker so later journeys see a healthy stack.
      await expect
        .poll(async () => (await apiFetch(asOwner, "/api/employees?limit=1")).status, {
          message: "payroll reachable through the BFF again",
          timeout: 90_000,
          intervals: [1_000],
        })
        .toBe(200);
    });

    test("the employees page shows the error banner with a reference (PAY-602)", async ({
      asOwner,
    }) => {
      await setProxyEnabled("payroll", false);
      await asOwner.goto("/employees");
      const banner = alertWith(asOwner, "The service is temporarily unavailable.");
      await expect(banner).toBeVisible({ timeout: 30_000 });
      await expect(banner.getByText(/^Reference: [0-9a-f-]{36}$/)).toBeVisible();
      await expect(banner.getByRole("button", { name: "Retry" })).toBeVisible();
      const api = await apiFetch(asOwner, "/api/employees?limit=1");
      expect(api.status).toBe(503);
      expect(errorCode(api)).toBe("service_unavailable");
    });
  });

  test("a foreign Origin → 403 bad_origin; a 70 KB body → 413 payload_too_large (PAY-604)", async ({
    asOwner,
  }) => {
    await asOwner.goto("/employees");
    const evil = await asOwner.request.post("/api/employees", {
      data: employeeBody(),
      headers: { Origin: "https://evil.example" },
    });
    expect(evil.status()).toBe(403);
    expect(((await evil.json()) as { error: { code: string } }).error.code).toBe("bad_origin");

    const noOrigin = await asOwner.request.post("/api/employees", { data: employeeBody() });
    expect(noOrigin.status()).toBe(403);

    const big = await apiFetch(asOwner, "/api/employees", {
      method: "POST",
      body: employeeBody({ name: "x".repeat(70 * 1024) }),
    });
    expect(big.status).toBe(413);
    expect(errorCode(big)).toBe("payload_too_large");
  });

  test("security headers on a page and an /api route (PAY-604, ADR 0009)", async ({ asOwner }) => {
    const page = await asOwner.goto("/dashboard");
    const api = await asOwner.request.get("/api/auth/me");
    expect(api.status()).toBe(200);
    for (const h of [page!.headers(), api.headers()]) {
      const csp = h["content-security-policy"] ?? "";
      expect(csp).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/);
      expect(csp).toContain("frame-ancestors 'none'");
      expect(h["x-frame-options"]).toBe("DENY");
      expect(h["x-content-type-options"]).toBe("nosniff");
      expect(h["referrer-policy"]).toBe("same-origin");
      expect(h["x-request-id"]).toMatch(/^[0-9a-f-]{36}$/);
      expect(h["strict-transport-security"]).toBeUndefined(); // dev: no HSTS
    }
    // Every BFF response carries the trace ID (web LLD §7.3) and is never cached.
    expect(api.headers()["x-trace-id"]).toMatch(/^[0-9a-f]{32}$/);
    expect(api.headers()["cache-control"]).toBe("no-store");
    // A fresh nonce per response.
    const again = await asOwner.request.get("/dashboard");
    expect(again.headers()["content-security-policy"]).not.toBe(
      page!.headers()["content-security-policy"],
    );
  });
});
