// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AppShell } from "@/components/app-shell";
import { CursorPager } from "@/components/cursor-pager";
import { ErrorBanner } from "@/components/error-banner";
import { Forbidden } from "@/components/forbidden";
import { LoginForm, safeNext } from "@/components/forms/login-form";
import { nextMonthStarts, RunCreateForm } from "@/components/forms/run-create-form";
import { RunList } from "@/components/runs/run-list";
import { routeTemplate, send, WebVitals } from "@/components/web-vitals";
import { ApiError, apiFetch } from "@/lib/client/api";

import { payRun, RUN_ID } from "../helpers/fixtures";
import { apiError, renderWithClient, stubApi } from "../helpers/ui";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("next/web-vitals", () => ({ useReportWebVitals: vi.fn() }));

const assign = vi.fn();
beforeEach(() => {
  assign.mockReset();
  push.mockReset();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, assign, pathname: "/pay-runs", search: "?x=1" },
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("LoginForm", () => {
  // PAY-104: the open-redirect guard on `next`.
  it("only follows same-origin paths", () => {
    expect(safeNext("/pay-runs/1")).toBe("/pay-runs/1");
    expect(safeNext("//evil.example")).toBe("/dashboard");
    expect(safeNext("/\\evil.example")).toBe("/dashboard");
    expect(safeNext("https://evil.example")).toBe("/dashboard");
    expect(safeNext(undefined)).toBe("/dashboard");
  });

  // PAY-104, C63: the URL parser strips tab/CR/LF and reads "\" as "/", so these would
  // otherwise become protocol-relative off-site redirects.
  it("refuses control characters and backslashes", () => {
    const o = "http://127.0.0.1:4100";
    expect(safeNext("/\t/evil.example", o)).toBe("/dashboard");
    expect(safeNext("/\n/evil.example", o)).toBe("/dashboard");
    expect(safeNext("/%5Cevil", o)).toBe("/%5Cevil"); // stays a same-origin path
    expect(safeNext("/pay-runs?tab=payouts#x", o)).toBe("/pay-runs?tab=payouts");
  });

  async function fill() {
    await userEvent.type(screen.getByLabelText("Email"), "owner@payrun.example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
  }

  // PAY-104: success goes to `next`.
  it("signs in and goes to next", async () => {
    const calls = stubApi((c) =>
      c.path === "/api/auth/login" ? { body: { user: {} } } : undefined,
    );
    render(<LoginForm next="/pay-runs" />);
    await fill();
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/pay-runs"));
    expect(calls[0]!.body).toEqual({
      email: "owner@payrun.example.com",
      password: "correct horse",
    });
  });

  // PAY-104: 401 shows the mapped message, with no redirect to /login.
  it("shows invalid_credentials", async () => {
    stubApi(() => apiError(401, "invalid_credentials", "Email or password is incorrect."));
    render(<LoginForm />);
    await fill();
    expect((await screen.findByRole("alert")).textContent).toBe("Email or password is incorrect.");
    expect(assign).not.toHaveBeenCalled();
  });

  // PAY-104: 429 → countdown from Retry-After.
  it("counts down after a 429", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubApi(() => ({
      ...apiError(429, "rate_limited", "Too many"),
      headers: { "retry-after": "3" },
    }));
    render(<LoginForm />);
    await fill();
    await screen.findByText("Too many requests. Try again in 3 seconds.");
    await act(() => vi.advanceTimersByTimeAsync(1000));
    await screen.findByText("Too many requests. Try again in 2 seconds.");
    expect((screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  // PAY-104: 503 → "try again"; an empty form isn't sent.
  it("handles 503 and an empty form", async () => {
    const calls = stubApi(() => apiError(503, "service_unavailable", "down"));
    render(<LoginForm />);
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByRole("alert").textContent).toBe("Enter your email and password.");
    expect(calls).toHaveLength(0);
    await fill();
    expect((await screen.findByRole("alert")).textContent).toMatch(/temporarily unavailable/);
  });
});

describe("RunCreateForm", () => {
  // PAY-300: monthly suggestions are first-of-month dates.
  it("suggests first-of-month dates across a year end", () => {
    expect(nextMonthStarts(new Date(2026, 10, 10))).toEqual([
      "2026-11-01",
      "2026-12-01",
      "2027-01-01",
    ]);
  });

  // PAY-300: creates a monthly run and goes to it.
  it("creates a run", async () => {
    const calls = stubApi((c) =>
      c.method === "POST" ? { status: 201, body: payRun({ status: "draft" }) } : undefined,
    );
    renderWithClient(<RunCreateForm />);
    await userEvent.click(screen.getByRole("button", { name: "Create pay run" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/pay-runs/${RUN_ID}`));
    expect(calls[0]!.body).toMatchObject({
      schedule: "monthly",
      period_start: expect.stringMatching(/-01$/),
    });
  });

  // PAY-300: pay_run_exists shows its message and reference.
  it("shows pay_run_exists", async () => {
    stubApi(() =>
      apiError(409, "pay_run_exists", "A pay run already exists for this schedule and period."),
    );
    renderWithClient(<RunCreateForm />);
    await userEvent.click(screen.getByRole("button", { name: "Create pay run" }));
    expect((await screen.findByRole("alert")).textContent).toContain("A pay run already exists");
  });

  // PAY-602 (§6.1): no response at all still shows a message.
  it("shows a message on a network error", async () => {
    stubApi(() => new TypeError("Failed to fetch"));
    renderWithClient(<RunCreateForm />);
    await userEvent.click(screen.getByRole("button", { name: "Create pay run" }));
    expect((await screen.findByRole("alert")).textContent).toContain("temporarily unavailable");
  });
});

describe("RunList", () => {
  // PAY-303, C8: rows, and the pager follows next_cursor and disables Next on null.
  it("lists runs and pages by cursor", async () => {
    const calls = stubApi((c) =>
      c.path.includes("cursor=c2")
        ? {
            body: {
              items: [payRun({ period_start: "2026-10-01", period_end: "2026-10-31" })],
              next_cursor: null,
            },
          }
        : { body: { items: [payRun()], next_cursor: "c2" } },
    );
    renderWithClient(<RunList canCreate />);
    await screen.findByText("2026-11-01 – 2026-11-30");
    expect(screen.getByRole("link", { name: "New pay run" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("2026-10-01 – 2026-10-31");
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
    expect(calls.at(-1)!.path).toBe("/api/pay-runs?limit=50&cursor=c2");
    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    await screen.findByText("2026-11-01 – 2026-11-30");
  });

  // PAY-601 (UI convenience): no "New pay run" without payroll_admin; 503 banner with retry.
  it("hides create and shows the 503 banner", async () => {
    stubApi(() => apiError(503, "service_unavailable", "down", "ref-9"));
    renderWithClient(<RunList canCreate={false} />);
    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(alert.textContent).toContain("The service is temporarily unavailable.");
    expect(alert.textContent).toContain("Reference: ref-9");
    expect(screen.queryByRole("link", { name: "New pay run" })).toBeNull();
  });
});

describe("apiFetch", () => {
  // PAY-106, §6.1: a 401 sends the user to /login?next=<path>.
  it("redirects on 401 unauthenticated", async () => {
    stubApi(() => apiError(401, "unauthenticated", "Your session has ended."));
    await expect(apiFetch("/api/pay-runs")).rejects.toBeInstanceOf(ApiError);
    expect(assign).toHaveBeenCalledWith("/login?next=%2Fpay-runs%3Fx%3D1");
  });

  // PAY-602: a non-JSON error keeps a generic message; 204 returns undefined; /api only.
  it("handles non-JSON errors, 204 and non-API paths", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () => new Response("<html>", { status: 502, headers: { "x-request-id": "rid" } }),
      ),
    );
    await expect(apiFetch("/api/x")).rejects.toMatchObject({
      status: 502,
      code: "request_failed",
      requestId: "rid",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 204 })),
    );
    await expect(apiFetch("/api/x", { method: "POST" })).resolves.toBeUndefined();
    await expect(apiFetch("https://evil.example/api")).rejects.toThrow("/api/* paths only");
  });
});

describe("shell and shared UI", () => {
  // PAY-106: sign out posts to logout, then goes to /login.
  it("signs out", async () => {
    const calls = stubApi((c) => (c.path === "/api/auth/logout" ? { status: 204 } : undefined));
    render(
      <AppShell user={{ id: "u", name: "Olive", roles: ["approver"] }}>
        <p>content</p>
      </AppShell>,
    );
    expect(screen.getByRole("link", { name: "Skip to content" }).getAttribute("href")).toBe(
      "#main",
    );
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
    expect(calls[0]!.method).toBe("POST");
  });

  // PAY-602: the banner shows the message, the reference and Retry.
  it("ErrorBanner, Forbidden and CursorPager", async () => {
    const retry = vi.fn();
    render(
      <ErrorBanner
        error={new ApiError(409, "x", "Mapped message", "r-1", undefined, null)}
        onRetry={retry}
      />,
    );
    expect(screen.getByText("Mapped message")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalled();
    cleanup();
    render(<ErrorBanner error={new Error("raw internal")} />);
    expect(screen.queryByText("raw internal")).toBeNull();
    cleanup();
    render(<Forbidden />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toMatch(/access/);
    cleanup();
    const next = vi.fn();
    render(<CursorPager nextCursor="n" hasPrevious={false} onNext={next} onPrevious={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(next).toHaveBeenCalledWith("n");
  });

  // §7.5, PAY-NFR-07: telemetry uses route templates and sendBeacon, falling back to fetch.
  it("sends telemetry with route templates", async () => {
    expect(routeTemplate("/pay-runs/0193a000-0000-7000-8000-0000000000b1")).toBe("/pay-runs/[id]");
    expect(routeTemplate("/Weird Path")).toBe("/[other]");
    const beacon = vi.fn(() => false);
    Object.defineProperty(navigator, "sendBeacon", { configurable: true, value: beacon });
    const calls = stubApi(() => ({ status: 204 }));
    send({ type: "error", message: "m", route: "/" });
    expect(beacon).toHaveBeenCalled();
    await waitFor(() => expect(calls).toHaveLength(1));
    render(<WebVitals />);
    window.dispatchEvent(new ErrorEvent("error", { message: "boom" }));
    await waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    expect(calls.at(-1)!.body).toEqual({ type: "error", message: "boom", route: "/pay-runs" });
  });
});
