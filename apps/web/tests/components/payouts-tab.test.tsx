// @vitest-environment jsdom
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PayoutsTab } from "@/components/runs/payouts-tab";

import { attempt, payout, RUN_ID } from "../helpers/fixtures";
import { apiError, renderWithClient, stubApi } from "../helpers/ui";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const payoutsPath = `/api/pay-runs/${RUN_ID}/payouts`;
const FAILED_ID = "0193a000-0000-7000-8000-0000000000d4";
const BLOCKED_ID = "0193a000-0000-7000-8000-0000000000d3";

const failed = payout({
  id: FAILED_ID,
  employee_name: "Di",
  status: "failed",
  current_attempt: 2,
  attempts: [
    attempt({
      attempt: 1,
      status: "rejected",
      failure_reason: "account_closed",
      bank_reference: null,
    }),
    attempt({
      attempt: 2,
      status: "rejected",
      failure_reason: "operator_rejected",
      account_last4: "4321",
      bank_reference: null,
    }),
  ],
});
const blocked = payout({
  id: BLOCKED_ID,
  employee_name: "Cy",
  status: "requested",
  attempts: [attempt({ status: "needs_operator", final_at: null, bank_reference: null })],
});
const paid = payout();
const noTransfer = payout({
  id: "0193a000-0000-7000-8000-0000000000d5",
  employee_name: "Zed",
  amount_cents: 0,
  no_transfer: true,
  current_attempt: 0,
  attempts: [],
});

function render(over: Partial<Parameters<typeof PayoutsTab>[0]> = {}) {
  const onOpenPayslip = vi.fn();
  const onNotice = vi.fn();
  const r = renderWithClient(
    <PayoutsTab
      runId={RUN_ID}
      pollMs={false}
      canAct
      onOpenPayslip={onOpenPayslip}
      onNotice={onNotice}
      {...over}
    />,
  );
  return { ...r, onOpenPayslip, onNotice };
}

function rowOf(name: string) {
  return screen.getByRole("button", { name }).closest("tbody") as HTMLElement;
}

describe("PayoutsTab", () => {
  // PAY-500, PAY-502: Retry only on failed rows, Resolve only on Blocked rows, for approvers.
  it("offers Retry on failed rows and Resolve on blocked rows", async () => {
    stubApi((c) =>
      c.path.startsWith(payoutsPath)
        ? { body: { items: [failed, blocked, paid, noTransfer], next_cursor: null } }
        : undefined,
    );
    render();
    await screen.findByText("Di");
    expect(within(rowOf("Di")).getByRole("button", { name: "Retry payout to Di" })).toBeTruthy();
    expect(within(rowOf("Di")).queryByRole("button", { name: /Resolve/ })).toBeNull();
    expect(within(rowOf("Cy")).getByRole("button", { name: "Resolve payout to Cy" })).toBeTruthy();
    expect(within(rowOf("Cy")).queryByRole("button", { name: /Retry/ })).toBeNull();
    expect(
      within(rowOf("Ada Employee")).queryByRole("button", { name: /Retry|Resolve/ }),
    ).toBeNull();
    // PAY-501: the current attempt's last4, the badge with its reason, and "no transfer"
    expect(within(rowOf("Di")).getByText("••••4321")).toBeTruthy();
    expect(within(rowOf("Di")).getByText("Rejected by an operator")).toBeTruthy();
    expect(within(rowOf("Zed")).getByText("Paid (no transfer)")).toBeTruthy();
  });

  // PAY-601: no actions for a user without the approver role.
  it("hides actions without the approver role", async () => {
    stubApi(() => ({ body: { items: [failed, blocked], next_cursor: null } }));
    render({ canAct: false });
    await screen.findByText("Di");
    expect(screen.queryByRole("button", { name: /Retry|Resolve/ })).toBeNull();
  });

  // PAY-501: the History disclosure lists every attempt with reason, reference and last4.
  it("shows the attempt history on demand", async () => {
    stubApi(() => ({ body: { items: [failed, noTransfer], next_cursor: null } }));
    render();
    await screen.findByText("Di");
    const toggle = within(rowOf("Di")).getByRole("button", { name: "History" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await userEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const history = screen.getByRole("table", { name: "Attempts for Di" });
    const rows = within(history).getAllByRole("row");
    expect(rows).toHaveLength(3); // header + 2 attempts
    expect(rows[1]!.textContent).toContain("Account closed");
    expect(rows[2]!.textContent).toContain("••••4321");
    await userEvent.click(within(rowOf("Zed")).getByRole("button", { name: "History" }));
    expect(screen.getByText("No transfer was needed.")).toBeTruthy();
  });

  // PAY-501, C8: the status filter passes status and resets the pager; Next follows next_cursor.
  it("filters by status and pages by cursor", async () => {
    const calls = stubApi((c) => {
      const u = new URL(c.path, "http://x");
      if (u.searchParams.get("cursor") === "n2")
        return { body: { items: [paid], next_cursor: null } };
      return { body: { items: [failed], next_cursor: "n2" } };
    });
    render();
    await screen.findByText("Di");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Ada Employee");
    expect(calls.at(-1)!.path).toBe(`${payoutsPath}?limit=50&cursor=n2`);
    await userEvent.selectOptions(screen.getByLabelText("Show"), "failed");
    await waitFor(() => expect(calls.at(-1)!.path).toBe(`${payoutsPath}?limit=50&status=failed`));
    expect((screen.getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  // PAY-303: the employee name opens the payslip by payslip_id.
  it("links rows to their payslip", async () => {
    stubApi(() => ({ body: { items: [paid], next_cursor: null } }));
    const { onOpenPayslip } = render();
    await userEvent.click(await screen.findByRole("button", { name: "Ada Employee" }));
    expect(onOpenPayslip).toHaveBeenCalledWith(paid.payslip_id);
  });

  // PAY-413: the visible page polls at the given interval.
  it("polls while pollMs is set", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = stubApi(() => ({ body: { items: [blocked], next_cursor: null } }));
    render({ pollMs: 2000 });
    await screen.findByText("Cy");
    const before = calls.length;
    await act(() => vi.advanceTimersByTimeAsync(4100));
    expect(calls.length).toBeGreaterThanOrEqual(before + 2);
  });

  // PAY-501: a load failure shows the banner.
  it("shows an error banner when the page can't load", async () => {
    stubApi(() => apiError(404, "pay_run_not_found", "That pay run wasn't found."));
    render();
    expect((await screen.findByRole("alert")).textContent).toMatch(/wasn't found/);
  });
});

describe("RetryButton (through PayoutsTab)", () => {
  // PAY-500, C52: a new Idempotency-Key per click; success invalidates the run and notifies.
  it("sends a fresh key per click", async () => {
    let n = 0;
    const calls = stubApi((c) => {
      if (c.method === "POST") {
        n++;
        return n === 1
          ? apiError(503, "service_unavailable", "The service is temporarily unavailable.")
          : { body: payout({ id: FAILED_ID, status: "requested" }) };
      }
      return { body: { items: [failed], next_cursor: null } };
    });
    const { onNotice, client } = render();
    const spy = vi.spyOn(client, "invalidateQueries");
    const btn = await screen.findByRole("button", { name: "Retry payout to Di" });
    await userEvent.click(btn);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/temporarily unavailable/);
    expect(btn.getAttribute("aria-describedby")).toBe(alert.id); // PAY-NFR-14
    await userEvent.click(btn);
    await waitFor(() => expect(onNotice).toHaveBeenCalledWith("Retry sent for Di."));
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(2);
    expect(posts[0]!.path).toBe(`${payoutsPath}/${FAILED_ID}/retry`);
    const k1 = posts[0]!.headers["Idempotency-Key"];
    const k2 = posts[1]!.headers["Idempotency-Key"];
    expect(k1).toMatch(/^[0-9a-f-]{36}$/);
    expect(k2).not.toBe(k1);
    expect(spy).toHaveBeenCalledWith({ queryKey: ["pay-run", RUN_ID] });
  });

  // PAY-500, PAY-401: the retry approver_must_differ message renders in role="alert".
  it("shows the retry dual-approval message", async () => {
    const msg =
      "Dual approval is on: someone else must retry. You marked this payment rejected, or changed this employee's bank details and a second person hasn't released them yet.";
    stubApi((c) =>
      c.method === "POST"
        ? apiError(403, "approver_must_differ", msg)
        : { body: { items: [failed], next_cursor: null } },
    );
    render();
    await userEvent.click(await screen.findByRole("button", { name: "Retry payout to Di" }));
    expect((await screen.findByRole("alert")).textContent).toContain(msg);
  });
});
