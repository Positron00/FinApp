// @vitest-environment jsdom
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ApproveButton } from "@/components/runs/approve-button";
import type { PayRun } from "@/lib/schemas/responses";

import { payRun, RUN_ID } from "../helpers/fixtures";
import { USER_ID } from "../helpers/request";
import { apiError, renderWithClient, stubApi } from "../helpers/ui";

const OTHER = "0193a000-0000-7000-8000-0000000000a9";
const run = payRun({ calculated_by: OTHER }) as PayRun;
const approvePath = `/api/pay-runs/${RUN_ID}/approve`;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function clickApprove() {
  await userEvent.click(screen.getByRole("button", { name: "Approve" }));
  await userEvent.click(screen.getByRole("button", { name: "Confirm approval" }));
}

describe("ApproveButton", () => {
  // PAY-400: a new UUID Idempotency-Key per click.
  it("sends a fresh key per click", async () => {
    const calls = stubApi((c) =>
      c.path === approvePath
        ? apiError(
            409,
            "pay_run_not_approvable",
            "This run can't be approved: it has already been approved, or it isn't calculated.",
          )
        : undefined,
    );
    renderWithClient(<ApproveButton run={run} meId={USER_ID} />);
    await clickApprove();
    await screen.findByRole("alert");
    await clickApprove();
    await waitFor(() => expect(calls).toHaveLength(2));
    const [a, b] = calls.map((c) => c.headers["Idempotency-Key"]);
    expect(a).toMatch(/^[0-9a-f-]{36}$/);
    expect(b).toMatch(/^[0-9a-f-]{36}$/);
    expect(a).not.toBe(b);
  });

  // PAY-400: the confirm shows funding and employee count; disabled while pending (one request).
  it("confirms with the funding total and is disabled while pending", async () => {
    let release: (() => void) | undefined;
    const calls = stubApi(() => undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (path: string, init: RequestInit) =>
          new Promise<Response>((resolve) => {
            calls.push({ method: init.method ?? "GET", path, headers: {}, body: undefined });
            release = () =>
              resolve(
                new Response(JSON.stringify(payRun({ status: "processing" })), { status: 200 }),
              );
          }),
      ),
    );
    renderWithClient(<ApproveButton run={run} meId={USER_ID} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(screen.getByText(/Approve paying 5 employee\(s\), funding \$18,750.00\?/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Confirm approval" }));
    const btn = screen.getByRole("button", { name: "Approve" });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(btn);
    expect(calls).toHaveLength(1);
    release!();
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
  });

  // PAY-401: the dual-approval message in role="alert", linked to the button.
  it("shows approver_must_differ in an alert", async () => {
    const msg =
      "Dual approval is on: you can't approve this run because you calculated it, or you changed bank details for an employee in it. Ask another approver.";
    stubApi(() => apiError(403, "approver_must_differ", msg));
    renderWithClient(<ApproveButton run={run} meId={USER_ID} />);
    await clickApprove();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(msg);
    expect(alert.textContent).toContain("Reference: req-ui-1");
    expect(screen.getByRole("button", { name: "Approve" }).getAttribute("aria-describedby")).toBe(
      alert.id,
    );
  });

  // PAY-401: the hint when the current user calculated a dual-approval run.
  it("hints when the user calculated the run", () => {
    stubApi(() => undefined);
    renderWithClient(
      <ApproveButton run={payRun({ calculated_by: USER_ID }) as PayRun} meId={USER_ID} />,
    );
    expect(screen.getByText(/You calculated this run/)).toBeTruthy();
  });

  // PAY-400: a network error refetches the run; processing means it landed.
  it("treats a network error as success when the run is processing", async () => {
    stubApi((c) => {
      if (c.path === approvePath) return new TypeError("Failed to fetch");
      if (c.path === `/api/pay-runs/${RUN_ID}`) return { body: payRun({ status: "processing" }) };
      return undefined;
    });
    renderWithClient(<ApproveButton run={run} meId={USER_ID} />);
    await clickApprove();
    await screen.findByText("Approved. Payouts are on their way.");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  // PAY-400: a network error with the run still calculated shows the error; the user can retry.
  it("shows an error when the network error didn't land", async () => {
    stubApi((c) => {
      if (c.path === approvePath) return new TypeError("Failed to fetch");
      if (c.path === `/api/pay-runs/${RUN_ID}`) return { body: payRun({ status: "calculated" }) };
      return undefined;
    });
    renderWithClient(<ApproveButton run={run} meId={USER_ID} />);
    await clickApprove();
    expect((await screen.findByRole("alert")).textContent).toMatch(/couldn't be confirmed/);
  });
  // PAY-NFR-14 (web LLD §6.3): the confirm is a dialog; focus moves in, Escape closes it and focus
  // returns to Approve, which stays mounted throughout.
  it("moves focus into the confirm dialog and back to Approve on Escape", async () => {
    const calls = stubApi(() => undefined);
    renderWithClient(<ApproveButton run={run} meId={USER_ID} />);
    const approveBtn = screen.getByRole("button", { name: "Approve" });
    approveBtn.focus();
    await userEvent.keyboard("{Enter}");
    const dialog = screen.getByRole("alertdialog", { name: "Confirm approval" });
    expect(screen.getByRole("button", { name: "Approve" })).toBe(approveBtn);
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Confirm approval" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(document.activeElement).toBe(approveBtn);
    expect(calls).toHaveLength(0);
  });

  // PAY-NFR-14: Cancel closes the dialog without a request and returns focus to Approve.
  it("cancels without a request and returns focus", async () => {
    const calls = stubApi(() => undefined);
    renderWithClient(<ApproveButton run={run} meId={USER_ID} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Approve" }));
    expect(calls).toHaveLength(0);
  });
});
