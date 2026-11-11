// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { announcement, RunDetail, RunTabs, who } from "@/components/runs/run-detail";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

import { attempt, payout, payRun, payslip, RUN_ID } from "../helpers/fixtures";
import { USER_ID } from "../helpers/request";
import { apiError, renderWithClient, stubApi } from "../helpers/ui";

const ME = {
  id: USER_ID,
  roles: ["payroll_admin", "approver"] as ("payroll_admin" | "approver")[],
};
const OTHER = "0193a000-0000-7000-8000-0000000000a9";
const runPath = `/api/pay-runs/${RUN_ID}`;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function page<T>(items: T[]) {
  return { items, next_cursor: null };
}

describe("RunTabs", () => {
  // PAY-NFR-14: the WAI-ARIA tabs pattern: roving tabIndex, arrow keys, Home and End.
  it("moves selection and focus with the arrow keys", async () => {
    function Harness() {
      const [active, setActive] = useState<"payslips" | "payouts">("payslips");
      return <RunTabs active={active} onSelect={setActive} />;
    }
    render(<Harness />);
    const [slips, outs] = screen.getAllByRole("tab");
    expect(slips!.getAttribute("tabindex")).toBe("0");
    expect(outs!.getAttribute("tabindex")).toBe("-1");
    slips!.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(outs!.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(outs);
    expect(slips!.getAttribute("tabindex")).toBe("-1");
    await userEvent.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(slips);
    await userEvent.keyboard("{End}");
    expect(document.activeElement).toBe(outs);
    await userEvent.keyboard("{Home}");
    expect(document.activeElement).toBe(slips);
    expect(slips!.getAttribute("aria-controls")).toBe("panel-payslips");
  });
});

describe("RunDetail", () => {
  // PAY-413: polls the run every 2 s while processing and stops at paid.
  it("polls while processing and stops at a final status", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let runCalls = 0;
    const calls = stubApi((c) => {
      if (c.path === runPath) {
        runCalls++;
        const status = runCalls < 3 ? "processing" : "paid";
        return {
          body: payRun({
            status,
            approved_at: "2026-11-10T12:01:00Z",
            approved_by: OTHER,
            approved_by_name: "Ann Approver",
            counts: {
              requested: 3 - runCalls,
              awaiting_bank: 0,
              blocked: 0,
              paid: runCalls,
              no_transfer: 0,
              failed: 0,
            },
          }),
        };
      }
      if (c.path.startsWith(`${runPath}/payouts`)) return { body: page([payout()]) };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={ME} />);
    await screen.findByText(/Paying employees: 1 of 3 paid/);
    await act(() => vi.advanceTimersByTimeAsync(2000));
    await screen.findByText(/Paying employees: 2 of 3 paid/);
    await act(() => vi.advanceTimersByTimeAsync(2000));
    await waitFor(() => expect(screen.getAllByText("Paid").length).toBeGreaterThan(0));
    const before = runCalls;
    await act(() => vi.advanceTimersByTimeAsync(6000));
    expect(runCalls).toBe(before); // polling stopped
    expect(screen.queryByText(/Paying employees/)).toBeNull();
    // the payouts tab (default after approval) refetched once at the final status
    expect(
      calls.filter((c) => c.path.startsWith(`${runPath}/payouts`)).length,
    ).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/^Ann Approver, /)).toBeTruthy();
  });

  // PAY-413, R21: the banner comes from counts.awaiting_bank / counts.blocked with no payouts request.
  it("shows the awaiting-bank and blocked banner from counts only", async () => {
    const calls = stubApi((c) => {
      if (c.path === runPath) {
        return {
          body: payRun({
            status: "processing",
            counts: {
              requested: 4,
              awaiting_bank: 1,
              blocked: 2,
              paid: 6,
              no_transfer: 1,
              failed: 0,
            },
          }),
        };
      }
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([payslip()]) };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={ME} />);
    await screen.findByText("Paying employees: 6 of 10 paid");
    expect(screen.getByText("1 awaiting bank")).toBeTruthy();
    expect(screen.getByText(/2 blocked: an operator must resolve these/)).toBeTruthy();
    expect(calls.some((c) => c.path.includes("/payouts"))).toBe(false);
  });

  // PAY-413, PAY-501: "Awaiting bank" and "Blocked" badges on the payouts tab.
  it("renders payout badges, amounts and last4", async () => {
    stubApi((c) => {
      if (c.path === runPath)
        return { body: payRun({ status: "processing", approved_at: "2026-11-10T12:00:00Z" }) };
      if (c.path.startsWith(`${runPath}/payouts`)) {
        return {
          body: page([
            payout({
              id: "0193a000-0000-7000-8000-0000000000d2",
              employee_name: "Bo",
              status: "requested",
              attempts: [attempt({ status: "indeterminate", final_at: null })],
            }),
            payout({
              id: "0193a000-0000-7000-8000-0000000000d3",
              employee_name: "Cy",
              status: "requested",
              attempts: [attempt({ status: "needs_operator", final_at: null })],
            }),
            payout({
              id: "0193a000-0000-7000-8000-0000000000d4",
              employee_name: "Di",
              status: "failed",
              attempts: [
                attempt({
                  status: "rejected",
                  failure_reason: "account_closed",
                  account_last4: "0000",
                }),
              ],
            }),
          ]),
        };
      }
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={ME} />);
    await screen.findByText("Awaiting bank");
    expect(screen.getByText("Blocked").closest("span")?.getAttribute("title")).toMatch(
      /operator must resolve/,
    );
    expect(screen.getByText("Account closed")).toBeTruthy();
    expect(screen.getByText("••••0000")).toBeTruthy();
    expect(screen.getAllByText("$3,750.00").length).toBe(3);
  });

  // PAY-303: header totals and "you" for the current user; payslip amounts.
  it("shows the header, 'you', and the payslips with run totals", async () => {
    stubApi((c) => {
      if (c.path === runPath) return { body: payRun({ calculated_by: USER_ID }) };
      if (c.path.startsWith(`${runPath}/payslips`))
        return {
          body: page([
            payslip({
              status: "pending",
              gross_cents: null,
              tax_cents: null,
              retirement_cents: null,
              net_cents: null,
            }),
          ]),
        };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={ME} />);
    await screen.findByText("Ada Employee");
    expect(screen.getByText(/^you, /)).toBeTruthy(); // calculated_by is me (§6.1)
    expect(screen.getAllByText("$18,750.00").length).toBeGreaterThanOrEqual(2); // net and funding
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(4); // pending amounts
    expect(screen.getByText("Dual approval")).toBeTruthy();
  });

  // PAY-302: calculate replaces the run in the cache.
  it("calculates a draft run", async () => {
    const calls = stubApi((c) => {
      if (c.method === "POST" && c.path === `${runPath}/calculate`)
        return { body: payRun({ status: "calculated" }) };
      if (c.path === runPath) return { body: payRun({ status: "draft", totals: null }) };
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([]) };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={{ id: USER_ID, roles: ["payroll_admin"] }} />);
    await userEvent.click(await screen.findByRole("button", { name: "Calculate" }));
    await screen.findByText("Calculated");
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull(); // not an approver
  });

  // PAY-302: calculate errors show the mapped message.
  it("shows a calculate error", async () => {
    stubApi((c) => {
      if (c.method === "POST")
        return apiError(409, "pay_run_locked", "This run is locked because it has been approved.");
      if (c.path === runPath) return { body: payRun({ status: "draft", totals: null }) };
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([]) };
      return undefined;
    });
    renderWithClient(<RunDetail runId={RUN_ID} me={ME} />);
    await userEvent.click(await screen.findByRole("button", { name: "Calculate" }));
    expect((await screen.findByRole("alert")).textContent).toContain("locked");
  });

  // §6.1: a failed refetch keeps the snapshot, marks it, and disables the actions.
  it("disables actions when a refetch fails", async () => {
    let n = 0;
    stubApi((c) => {
      if (c.path === runPath) {
        n++;
        return n === 1
          ? { body: payRun({ status: "processing" }) }
          : apiError(503, "service_unavailable", "down");
      }
      if (c.path.startsWith(`${runPath}/payslips`)) return { body: page([]) };
      return undefined;
    });
    const { client } = renderWithClient(<RunDetail runId={RUN_ID} me={ME} />);
    await screen.findByText(/Paying employees/);
    await act(() => client.refetchQueries({ queryKey: ["pay-run", RUN_ID], exact: true }));
    await screen.findByText(/Updates paused/);
    expect(screen.getByText(/Paying employees/)).toBeTruthy(); // the last snapshot stays
  });

  // PAY-303: an initial load failure shows the error with its reference.
  it("shows an error banner when the run can't load", async () => {
    stubApi((c) =>
      c.path === runPath
        ? apiError(404, "pay_run_not_found", "That pay run wasn't found.")
        : undefined,
    );
    renderWithClient(<RunDetail runId={RUN_ID} me={ME} />);
    expect((await screen.findByRole("alert")).textContent).toContain("That pay run wasn't found.");
  });

  // PAY-NFR-14: the live region text changes only with the status or counts.
  it("announces status and counts", () => {
    const r = payRun({
      status: "paid",
      counts: { requested: 0, awaiting_bank: 0, blocked: 0, paid: 498, no_transfer: 0, failed: 2 },
    });
    expect(announcement(r as never)).toBe("Run paid: 498 paid, 2 failed");
    expect(announcement(payRun({ status: "draft" }) as never)).toBe("Run draft");
    expect(who(null, null, USER_ID)).toBe("—");
    expect(who(OTHER, null, USER_ID)).toBe("0193a000");
  });
});
