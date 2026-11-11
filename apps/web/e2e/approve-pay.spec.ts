import { randomUUID } from "node:crypto";

import { apiFetch, expect, test } from "./fixtures";
import {
  approve,
  createRun,
  enterAllHours,
  ensureClosedAccountEmployee,
  errorCode,
  FINAL,
  getSettings,
  prepareRun,
  waitForRun,
} from "./helpers/api";
import { alertWith, rowWith, runBadge } from "./helpers/ui";

/**
 * J4 approve and pay (web LLD §9.2): dual approval on; the owner calculates in the UI; the
 * owner's own Approve is refused with the exact dual-approval message; the approver approves;
 * the page polls (≤ 2 s apart) until the run is `partially_failed` because of the account
 * ending 0000; the failed row says "Account closed"; a second approve with a new key is 409;
 * two parallel approves on a fresh run → exactly one 200.
 * PAY-400, PAY-401, PAY-413, PAY-501, ADR 0005.
 */
const DUAL_APPROVAL =
  "Dual approval is on: you can't approve this run because you calculated it, or you changed bank details for an employee in it. Ask another approver.";

test.describe("@journeys J4 approve and pay", () => {
  test("dual approval, approve, poll to partially_failed with the 0000 account (PAY-400, 401, 413, 501)", async ({
    asOwner,
    asApprover,
  }) => {
    expect((await getSettings(asOwner)).require_dual_approval).toBe(true);
    const closed = await ensureClosedAccountEmployee(asOwner);
    const run = await createRun(asOwner, "biweekly");
    await enterAllHours(asOwner, run.id);
    const runPath = `/pay-runs/${run.id}`;

    await test.step("the owner calculates in the UI (PAY-302)", async () => {
      await asOwner.goto(runPath);
      await asOwner.getByRole("button", { name: "Calculate", exact: true }).click();
      await expect(runBadge(asOwner, "Calculated")).toBeVisible();
      await expect(asOwner.getByText("Dual approval", { exact: true })).toBeVisible();
    });

    await test.step("the owner's approve is refused with the dual-approval message (PAY-401)", async () => {
      await expect(
        asOwner.getByText(
          "You calculated this run. With dual approval on, another approver must approve it.",
        ),
      ).toBeVisible();
      await asOwner.getByRole("button", { name: "Approve", exact: true }).click();
      await asOwner.getByRole("button", { name: "Confirm approval" }).click();
      const msg = alertWith(asOwner, DUAL_APPROVAL);
      await expect(msg).toBeVisible();
      await expect(msg).toHaveText(new RegExp(`^${DUAL_APPROVAL.replace(/[.()]/g, "\\$&")}`));
      await expect(runBadge(asOwner, "Calculated")).toBeVisible();
    });

    const runPolls: number[] = [];
    asApprover.on("request", (r) => {
      if (r.method() === "GET" && new URL(r.url()).pathname === `/api/pay-runs/${run.id}`) {
        runPolls.push(Date.now());
      }
    });
    let approvedAt = 0;

    await test.step("the approver approves → processing (PAY-400)", async () => {
      await asApprover.goto(runPath);
      await expect(runBadge(asApprover, "Calculated")).toBeVisible();
      await asApprover.getByRole("button", { name: "Approve", exact: true }).click();
      await expect(
        asApprover.getByText(/^Approve paying \d+ employee\(s\), funding \$/),
      ).toBeVisible();
      const approved = asApprover.waitForResponse(
        (r) =>
          r.url().endsWith(`/api/pay-runs/${run.id}/approve`) && r.request().method() === "POST",
      );
      await asApprover.getByRole("button", { name: "Confirm approval" }).click();
      const res = await approved;
      expect(res.status()).toBe(200);
      expect(res.request().headers()["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
      approvedAt = Date.now();
      expect(["processing", "paid", "partially_failed"]).toContain(
        ((await res.json()) as { status: string }).status,
      );
    });

    await test.step("the page polls until partially_failed within 60 s (PAY-413)", async () => {
      await expect(runBadge(asApprover, "Partially failed")).toBeVisible({ timeout: 60_000 });
      const final = await waitForRun(asApprover, run.id, FINAL, 5_000);
      expect(final.status).toBe("partially_failed");
      expect(final.counts.failed).toBeGreaterThanOrEqual(1);
      // While processing, GET /api/pay-runs/{id} was sent at most 2 s apart (plus scheduling).
      const during = runPolls.filter((t) => t >= approvedAt);
      const gaps = during.slice(1).map((t, i) => t - during[i]!);
      test.info().annotations.push({ type: "poll gaps (ms)", description: gaps.join(", ") });
      for (const g of gaps.slice(0, -1)) expect(g).toBeLessThanOrEqual(2_500);
    });

    await test.step("the failed row: Failed, Account closed, ••••0000, Retry offered (PAY-501)", async () => {
      const panel = asApprover.getByRole("tabpanel", { name: "Payouts" });
      const row = rowWith(panel, closed.name);
      // The badge ("Failed", icon aria-hidden) and the reason share the status cell.
      await expect(
        row.getByRole("cell", { name: "Failed Account closed", exact: true }),
      ).toBeVisible();
      await expect(row.getByRole("cell", { name: "••••0000", exact: true })).toBeVisible();
      await expect(
        row.getByRole("button", { name: `Retry payout to ${closed.name}` }),
      ).toBeVisible();
      await row.getByRole("button", { name: "History" }).click();
      const history = panel.getByRole("table", { name: `Attempts for ${closed.name}` });
      await expect(rowWith(history, /Rejected.*Account closed/)).toHaveCount(1);
    });

    await test.step("a second approve with a new key → 409 pay_run_not_approvable (PAY-400)", async () => {
      const again = await approve(asApprover, run.id, randomUUID());
      expect(again.status).toBe(409);
      expect(errorCode(again)).toBe("pay_run_not_approvable");
      const noKey = await apiFetch(asApprover, `/api/pay-runs/${run.id}/approve`, {
        method: "POST",
      });
      expect(noKey.status).toBe(400);
      expect(errorCode(noKey)).toBe("idempotency_key_required");
    });
  });

  test("two parallel approves on a fresh run → exactly one 200 (PAY-400)", async ({
    asOwner,
    asApprover,
  }) => {
    const run = await prepareRun(asOwner, "biweekly");
    await asApprover.goto(`/pay-runs/${run.id}`);
    const [a, b] = await Promise.all([approve(asApprover, run.id), approve(asApprover, run.id)]);
    expect([a.status, b.status].sort()).toEqual([200, 409]);
    expect([errorCode(a), errorCode(b)]).toContain("pay_run_not_approvable");
    // Let the payouts finish so later journeys start from a quiet worker.
    await waitForRun(asApprover, run.id, FINAL, 60_000);
  });
});
