// @vitest-environment jsdom
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  biweeklyStarts,
  derivedPeriodEnd,
  RunCreateForm,
} from "@/components/forms/run-create-form";
import { RunDetail } from "@/components/runs/run-detail";

import {
  attempt,
  EMPLOYEE_ID,
  payout,
  payRun,
  payslip,
  RUN_ID,
  settings,
} from "../helpers/fixtures";
import { USER_ID } from "../helpers/request";
import { apiError, renderWithClient, stubApi } from "../helpers/ui";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

beforeEach(() => push.mockReset());
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const runPath = `/api/pay-runs/${RUN_ID}`;
const PREPARER = { id: USER_ID, roles: ["payroll_admin"] as "payroll_admin"[] };
const page = <T,>(items: T[]) => ({ items, next_cursor: null });

describe("RunDetail actions", () => {
  // PAY-304: Delete asks first (alertdialog), then DELETEs and goes to /pay-runs.
  it("deletes a run after confirming", async () => {
    const calls = stubApi((c) => {
      if (c.method === "DELETE") return { status: 204 };
      if (c.path === runPath) return { body: payRun({ status: "calculated" }) };
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([]) };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={PREPARER} />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete run" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete this pay run?" });
    expect(document.activeElement?.textContent).toBe("Cancel"); // the safe choice has focus
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(within(dialog, "Delete run"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/pay-runs"));
    expect(calls.filter((c) => c.method === "DELETE").map((c) => c.path)).toEqual([runPath]);
  });

  // PAY-304: pay_run_locked on delete shows its message.
  it("shows a delete error", async () => {
    stubApi((c) => {
      if (c.method === "DELETE")
        return apiError(409, "pay_run_locked", "This run is locked because it has been approved.");
      if (c.path === runPath) return { body: payRun({ status: "draft", totals: null }) };
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([]) };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={PREPARER} />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete run" }));
    await userEvent.click(within(screen.getByRole("alertdialog"), "Delete run"));
    expect((await screen.findByRole("alert")).textContent).toMatch(/locked/);
    expect(push).not.toHaveBeenCalled();
  });

  // PAY-302, C15: hours_missing highlights the missing hourly rows in the hours editor.
  it("highlights hours_missing rows after calculate", async () => {
    const hourly = payslip({
      employee_id: EMPLOYEE_ID,
      employee_name: "Hal Hourly",
      pay_type: "hourly",
      annual_salary_cents: null,
      hourly_rate_cents: 2500,
      hours_hundredths: null,
      status: "pending",
    });
    stubApi((c) => {
      if (c.method === "POST")
        return {
          status: 422,
          body: {
            error: {
              code: "hours_missing",
              message: "Enter hours for every hourly employee before calculating (1 missing).",
              request_id: "r1",
              details: { employee_ids: [EMPLOYEE_ID] },
            },
          },
        };
      if (c.path === runPath) return { body: payRun({ status: "draft", totals: null }) };
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([hourly]) };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={PREPARER} />);
    const input = await screen.findByLabelText("Hours for Hal Hourly");
    expect(input.getAttribute("aria-invalid")).toBeNull();
    const calc = screen.getByRole("button", { name: "Calculate" });
    await userEvent.click(calc);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/1 missing/);
    expect(calc.getAttribute("aria-describedby")).toBe(alert.id); // PAY-NFR-14
    expect(input.getAttribute("aria-invalid")).toBe("true");
  });

  // PAY-303: a payslip row opens the payslip dialog (GET …/payslips/[payslipId]).
  it("opens a payslip", async () => {
    const slip = payslip();
    const calls = stubApi((c) => {
      if (c.path === runPath) return { body: payRun() };
      if (c.path === `${runPath}/payslips/${slip.id}`) return { body: slip };
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([slip]) };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={{ id: USER_ID, roles: ["approver"] }} />);
    await userEvent.click(await screen.findByRole("button", { name: "Ada Employee" }));
    const dialog = await screen.findByRole("dialog", { name: "Payslip: Ada Employee" });
    expect(dialog.textContent).toContain("$3,750.00");
    expect(calls.some((c) => c.path === `${runPath}/payslips/${slip.id}`)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  // PAY-413: the blocked banner link opens the Payouts tab filtered to requested.
  it("opens the payouts tab filtered to requested from the banner", async () => {
    const calls = stubApi((c) => {
      if (c.path === runPath)
        return {
          body: payRun({
            status: "processing",
            counts: {
              requested: 1,
              awaiting_bank: 0,
              blocked: 1,
              paid: 0,
              no_transfer: 0,
              failed: 0,
            },
          }),
        };
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([]) };
      if (c.path.startsWith(`${runPath}/payouts`))
        return {
          body: page([
            payout({
              status: "requested",
              attempts: [attempt({ status: "needs_operator", final_at: null })],
            }),
          ]),
        };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={{ id: USER_ID, roles: ["approver"] }} />);
    await userEvent.click(await screen.findByRole("button", { name: "Show payouts" }));
    expect(screen.getByRole("tab", { name: "Payouts" }).getAttribute("aria-selected")).toBe("true");
    await screen.findByText("Blocked");
    expect(calls.find((c) => c.path.startsWith(`${runPath}/payouts`))!.path).toContain(
      "status=requested",
    );
    // PAY-502: an approver gets Resolve on the blocked row, which opens the dialog
    await userEvent.click(screen.getByRole("button", { name: "Resolve payout to Ada Employee" }));
    expect(screen.getByRole("alertdialog", { name: "Resolve blocked payout" })).toBeTruthy();
  });

  // §6.1: a 429 on refetch shows "Live updates paused".
  it("says live updates are paused on 429", async () => {
    let n = 0;
    stubApi((c) => {
      if (c.path === runPath) {
        n++;
        return n === 1
          ? { body: payRun({ status: "processing" }) }
          : { ...apiError(429, "rate_limited", "Too many"), headers: { "retry-after": "5" } };
      }
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([]) };
      return undefined;
    });
    const { client } = renderWithClient(<RunDetail runId={RUN_ID} me={PREPARER} />);
    await screen.findByText(/Paying employees/);
    await client.refetchQueries({ queryKey: ["pay-run", RUN_ID], exact: true });
    expect(await screen.findByText(/Live updates paused/)).toBeTruthy();
  });
});

/** The button named `name` inside `el`. */
function within(el: HTMLElement, name: string): HTMLElement {
  const b = Array.from(el.querySelectorAll("button")).find((x) => x.textContent === name);
  if (!b) throw new Error(`no button ${name}`);
  return b;
}

describe("RunCreateForm, both schedules", () => {
  // PAY-300: biweekly suggestions are anchor + 14k, starting with the period containing today.
  it("computes biweekly starts and period ends", () => {
    expect(biweeklyStarts("2026-01-05", new Date(2026, 10, 11))).toEqual([
      "2026-11-09",
      "2026-11-23",
      "2026-12-07",
    ]);
    // the anchor itself, and a date before the anchor (k < 0) still lands on the 14-day grid
    expect(biweeklyStarts("2026-01-05", new Date(2026, 0, 5), 1)).toEqual(["2026-01-05"]);
    expect(biweeklyStarts("2026-01-05", new Date(2025, 11, 30), 1)).toEqual(["2025-12-22"]);
    expect(derivedPeriodEnd("biweekly", "2026-11-09")).toBe("2026-11-22");
    expect(derivedPeriodEnd("monthly", "2026-02-01")).toBe("2026-02-28");
    expect(derivedPeriodEnd("monthly", "2028-02-01")).toBe("2028-02-29");
    expect(derivedPeriodEnd("monthly", "nope")).toBeNull();
  });

  // PAY-300: a biweekly run uses the settings anchor's suggestions and posts the schedule.
  it("creates a biweekly run from a suggestion", async () => {
    const calls = stubApi((c) => {
      if (c.path === "/api/settings") return { body: settings({ biweekly_anchor: "2026-01-05" }) };
      if (c.method === "POST")
        return { status: 201, body: payRun({ schedule: "biweekly", status: "draft" }) };
      return undefined;
    });
    renderWithClient(<RunCreateForm />);
    await screen.findByText(/Period ends/);
    await userEvent.selectOptions(screen.getByLabelText("Schedule"), "biweekly");
    expect(await screen.findByText(/every 14 days from 2026-01-05/)).toBeTruthy();
    const group = screen.getByRole("group", { name: "Suggested period starts" });
    const second = group.querySelectorAll("button")[1]!;
    await userEvent.click(second);
    expect(second.getAttribute("aria-pressed")).toBe("true");
    const start = second.textContent!;
    await userEvent.click(screen.getByRole("button", { name: "Create pay run" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/pay-runs/${RUN_ID}`));
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({
      schedule: "biweekly",
      period_start: start,
    });
  });

  // PAY-300: pay_run_exists links to the existing run found in the list.
  it("links to the existing run on pay_run_exists", async () => {
    const OTHER_RUN = "0193a000-0000-7000-8000-0000000000b9";
    stubApi((c) => {
      if (c.path === "/api/settings") return { body: settings() };
      if (c.method === "POST")
        return apiError(
          409,
          "pay_run_exists",
          "A pay run already exists for this schedule and period.",
        );
      if (c.path.startsWith("/api/pay-runs?"))
        return { body: page([payRun({ id: OTHER_RUN, period_start: "2026-11-01" })]) };
      return undefined;
    });
    renderWithClient(<RunCreateForm />);
    await userEvent.clear(screen.getByLabelText("Period start"));
    await userEvent.type(screen.getByLabelText("Period start"), "2026-11-01");
    await userEvent.click(screen.getByRole("button", { name: "Create pay run" }));
    const link = await screen.findByRole("link", { name: "Open the existing run" });
    expect(link.getAttribute("href")).toBe(`/pay-runs/${OTHER_RUN}`);
  });

  // PAY-300, C15: employee_in_overlapping_run lists the employees from details.employee_ids.
  it("lists overlapping employees", async () => {
    stubApi((c) => {
      if (c.method === "POST")
        return {
          status: 409,
          body: {
            error: {
              code: "employee_in_overlapping_run",
              message: "1 employee(s) are already in another run that covers these dates.",
              request_id: "r",
              details: { employee_ids: [EMPLOYEE_ID] },
            },
          },
        };
      return apiError(404, "company_settings_not_found", "x");
    });
    renderWithClient(<RunCreateForm />);
    await userEvent.click(screen.getByRole("button", { name: "Create pay run" }));
    const link = await screen.findByRole("link", { name: `Employee ${EMPLOYEE_ID.slice(0, 8)}` });
    expect(link.getAttribute("href")).toBe(`/employees/${EMPLOYEE_ID}`);
  });

  // PAY-300: a malformed date is refused on the client and focused.
  it("refuses a malformed date", async () => {
    const calls = stubApi(() => apiError(404, "company_settings_not_found", "x"));
    renderWithClient(<RunCreateForm />);
    await userEvent.clear(screen.getByLabelText("Period start"));
    await userEvent.click(screen.getByRole("button", { name: "Create pay run" }));
    expect(await screen.findByText("Enter a date as YYYY-MM-DD.")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("Period start"));
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });
});
