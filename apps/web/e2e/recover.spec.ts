import { randomUUID } from "node:crypto";

import type { Page } from "@playwright/test";

import { apiFetch, expect, test } from "./fixtures";
import {
  accountNumber,
  approve,
  ensureClosedAccountEmployee,
  errorCode,
  FINAL,
  getRun,
  ok,
  payouts,
  prepareRun,
  ROUTING,
  waitForRun,
  type Payout,
} from "./helpers/api";
import { payrollDirectUrl } from "./helpers/env";
import { mintToken } from "./helpers/mint-token";
import { resetProxies, setProxyEnabled } from "./helpers/toxiproxy";
import { alertWith, rowWith, runBadge, statusWith } from "./helpers/ui";

/**
 * J5 recover (web LLD §9.2).
 * - Failed payout: from a `partially_failed` run (account ending 0000), the owner changes the
 *   employee's bank details; the owner's own Retry is refused (dual approval, retry message);
 *   the approver retries → `paid`; the history shows attempt 1 rejected and 2 settled; a retry
 *   of a paid payout → 409 `payout_not_retryable`.
 * - Blocked payouts (PAY-502, R8, C44, C48, C53): with the bank-stub proxy down, two payouts of
 *   a fresh run are made `needs_operator` with a worker token; both show "Blocked" and no Retry;
 *   A is resolved with "Send again" (and a second resolve is 409 `payout_not_blocked`); B's
 *   "Mark rejected" is first refused with the `resolve_too_early` countdown, then accepted once
 *   `PAYOUT_MARK_REJECTED_MIN_WAIT_S` (120 s, production timing) has passed; B is retried and
 *   the run ends `paid`.
 * PAY-401, PAY-500, PAY-501, PAY-502.
 */
const RETRY_DUAL =
  "Dual approval is on: you can't retry this payout because you last changed this employee's bank details. Ask another approver.";

const payoutsPanel = (p: Page) => p.getByRole("tabpanel", { name: "Payouts" });

test.describe.configure({ mode: "serial" });

test.describe("@journeys J5 recover", () => {
  test.afterAll(async () => {
    await resetProxies();
  });

  test("fix bank details, retry as the other approver, run paid (PAY-500, 501, 401)", async ({
    asOwner,
    asApprover,
  }) => {
    await ensureClosedAccountEmployee(asOwner);
    const run = await prepareRun(asOwner, "biweekly");
    ok(await approve(asApprover, run.id), "approve");
    const failedRun = await waitForRun(asApprover, run.id, FINAL, 60_000);
    expect(failedRun.status).toBe("partially_failed");
    const failed = (await payouts(asOwner, run.id)).filter((p) => p.status === "failed");
    expect(failed.length).toBeGreaterThanOrEqual(1);
    const target = failed[0] as Payout;
    const newAccount = accountNumber();

    await test.step("the owner (payroll admin) changes the failed employee's bank details", async () => {
      await asOwner.goto(`/employees/${target.employee_id}`);
      await asOwner.getByRole("button", { name: "Change bank details" }).click();
      const dialog = asOwner.getByRole("dialog", { name: "Change bank details" });
      await dialog.getByLabel("Routing number").fill(ROUTING);
      await dialog.getByLabel("Account number").fill(newAccount);
      await dialog.getByRole("button", { name: "Save bank details" }).click();
      await expect(statusWith(asOwner, "Bank details changed.")).toBeVisible();
      // Any other failed employee (a 0000 account created by another journey) is fixed too.
      for (const other of failed.slice(1)) {
        ok(
          await apiFetch(asOwner, `/api/employees/${other.employee_id}`, {
            method: "PATCH",
            body: { bank_routing: ROUTING, bank_account: accountNumber() },
          }),
          "fix bank details",
        );
      }
    });

    await test.step("the owner's own Retry is refused with the retry message (PAY-401, PAY-500)", async () => {
      await asOwner.goto(`/pay-runs/${run.id}`);
      const row = rowWith(payoutsPanel(asOwner), target.employee_name);
      await row.getByRole("button", { name: `Retry payout to ${target.employee_name}` }).click();
      await expect(alertWith(row, RETRY_DUAL)).toBeVisible();
      await expect(runBadge(asOwner, "Partially failed")).toBeVisible();
    });

    await test.step("the approver retries → processing → paid (PAY-500)", async () => {
      await asApprover.goto(`/pay-runs/${run.id}`);
      for (const p of failed) {
        const row = rowWith(payoutsPanel(asApprover), p.employee_name);
        const retried = asApprover.waitForResponse(
          (r) => r.url().endsWith(`/payouts/${p.id}/retry`) && r.request().method() === "POST",
        );
        await row.getByRole("button", { name: `Retry payout to ${p.employee_name}` }).click();
        expect((await retried).status()).toBe(200);
      }
      await expect(runBadge(asApprover, "Paid")).toBeVisible({ timeout: 60_000 });
    });

    await test.step("history: attempt 1 rejected, attempt 2 settled to the new account (PAY-501)", async () => {
      const row = rowWith(payoutsPanel(asApprover), target.employee_name).first();
      await row.getByRole("button", { name: "History" }).click();
      const history = payoutsPanel(asApprover).getByRole("table", {
        name: `Attempts for ${target.employee_name}`,
      });
      await expect(history.getByRole("row").nth(1)).toContainText(/^1Rejected.*Account closed/);
      await expect(history.getByRole("row").nth(2)).toContainText(
        new RegExp(`^2Settled.*••••${newAccount.slice(-4)}`),
      );
    });

    await test.step("a retry of a paid payout → 409 payout_not_retryable (PAY-500)", async () => {
      const res = await apiFetch(asApprover, `/api/pay-runs/${run.id}/payouts/${target.id}/retry`, {
        method: "POST",
        headers: { "Idempotency-Key": randomUUID() },
      });
      expect(res.status).toBe(409);
      expect(errorCode(res)).toBe("payout_not_retryable");
    });
  });

  test("blocked payouts: resolve with send again, then mark rejected after the wait, retry → paid (PAY-502)", async ({
    asOwner,
    asApprover,
    request,
  }) => {
    // PAYOUT_MARK_REJECTED_MIN_WAIT_S is 120 s under production timing (C48, C53).
    test.setTimeout(330_000);

    await setProxyEnabled("bank-stub", false);
    const run = await prepareRun(asOwner, "biweekly");
    ok(await approve(asApprover, run.id), "approve");
    const list = await payouts(asApprover, run.id);
    expect(list.length).toBeGreaterThanOrEqual(2);
    const [a, b] = list as [Payout, Payout];

    await test.step("make A and B needs_operator with a worker token (poison; C44)", async () => {
      for (const p of [a, b]) {
        const token = mintToken({
          aud: "payroll",
          sub: "service:payout-worker",
          issuer: "payout-worker",
        });
        const res = await request.post(`${payrollDirectUrl()}/v1/internal/payouts/${p.id}/result`, {
          headers: {
            Authorization: `Bearer ${token}`,
            "Idempotency-Key": `e2e-${randomUUID()}:poison`,
            "X-Request-ID": randomUUID(),
          },
          data: { attempt: p.current_attempt, outcome: "poison", reason: "schema_invalid" },
        });
        expect(res.status()).toBe(200);
        expect(((await res.json()) as { outcome: string }).outcome).toBe("needs_operator");
      }
    });

    await test.step("both rows are Blocked, the banner says 2 blocked, Retry isn't offered (PAY-413)", async () => {
      await asApprover.goto(`/pay-runs/${run.id}`);
      await expect(
        asApprover.getByText("2 blocked: an operator must resolve these."),
      ).toBeVisible();
      for (const p of [a, b]) {
        const row = rowWith(payoutsPanel(asApprover), p.employee_name);
        await expect(row.getByRole("cell", { name: "Blocked", exact: true })).toBeVisible();
        await expect(row.getByRole("button", { name: /^Retry payout/ })).toHaveCount(0);
        await expect(
          row.getByRole("button", { name: `Resolve payout to ${p.employee_name}` }),
        ).toBeVisible();
      }
    });

    await setProxyEnabled("bank-stub", true);

    await test.step("A: Send again (redrive) after the bank-check warning (PAY-502)", async () => {
      const row = rowWith(payoutsPanel(asApprover), a.employee_name);
      await row.getByRole("button", { name: `Resolve payout to ${a.employee_name}` }).click();
      const dialog = asApprover.getByRole("alertdialog", { name: "Resolve blocked payout" });
      await expect(dialog.getByRole("heading", { name: "Check the bank first" })).toBeFocused();
      const submit = dialog.getByRole("button", { name: "Resolve", exact: true });
      await expect(submit).toBeDisabled(); // no choice and no note yet
      await dialog.getByRole("radio", { name: /^Send again \(redrive\)/ }).check();
      await dialog
        .getByLabel(/^Note for the audit log/)
        .fill("Bank shows no transfer for the key.");
      await submit.click();
      await expect(dialog).toHaveCount(0);
      await expect(statusWith(asApprover, "Payout resent")).toBeVisible();
    });

    await test.step("a second resolve on A → 409 payout_not_blocked (PAY-502)", async () => {
      const res = await apiFetch(asApprover, `/api/pay-runs/${run.id}/payouts/${a.id}/resolve`, {
        method: "POST",
        body: { action: "redrive", note: "again" },
      });
      expect(res.status).toBe(409);
      expect(errorCode(res)).toBe("payout_not_blocked");
    });

    await test.step("B: Mark rejected is too early; the dialog counts down (C48)", async () => {
      const row = rowWith(payoutsPanel(asApprover), b.employee_name);
      await row.getByRole("button", { name: `Resolve payout to ${b.employee_name}` }).click();
      const dialog = asApprover.getByRole("alertdialog", { name: "Resolve blocked payout" });
      await dialog.getByRole("radio", { name: /^Mark rejected/ }).check();
      await dialog.getByLabel(/^Note for the audit log/).fill("Bank shows nothing settled.");
      const tooEarly = asApprover.waitForResponse((r) =>
        r.url().endsWith(`/payouts/${b.id}/resolve`),
      );
      await dialog.getByRole("button", { name: "Resolve", exact: true }).click();
      const res = await tooEarly;
      expect(res.status()).toBe(409);
      expect(Number(res.headers()["retry-after"])).toBeGreaterThan(0);
      await expect(
        alertWith(
          dialog,
          /^This payout was blocked moments ago\. Wait \d+ seconds, check the bank again, then mark it rejected\./,
        ),
      ).toBeVisible();
      await expect(dialog.getByRole("radio", { name: /^Mark rejected/ })).toBeDisabled();
      await expect(dialog.getByText(/Available in \d+ seconds\./)).toBeVisible();
    });

    await test.step("B: after the wait, Mark rejected succeeds → Failed, Rejected by an operator (PAY-502)", async () => {
      const dialog = asApprover.getByRole("alertdialog", { name: "Resolve blocked payout" });
      await expect(async () => {
        if ((await dialog.count()) === 0) return; // accepted on an earlier pass
        const radio = dialog.getByRole("radio", { name: /^Mark rejected/ });
        await expect(radio).toBeEnabled({ timeout: 1_000 });
        await radio.check();
        await dialog.getByRole("button", { name: "Resolve", exact: true }).click();
        await expect(dialog).toHaveCount(0, { timeout: 3_000 });
      }).toPass({ timeout: 150_000, intervals: [1_000] });
      await expect(statusWith(asApprover, "Payout marked rejected")).toBeVisible();
      const row = rowWith(payoutsPanel(asApprover), b.employee_name);
      await expect(
        row.getByRole("cell", { name: "Failed Rejected by an operator", exact: true }),
      ).toBeVisible();
      await row.getByRole("button", { name: `Retry payout to ${b.employee_name}` }).click();
    });

    await test.step("the run ends paid; A kept attempt 1, B settled on attempt 2 (PAY-500, 502)", async () => {
      await expect(runBadge(asApprover, "Paid")).toBeVisible({ timeout: 120_000 });
      const final = await getRun(asApprover, run.id);
      expect(final.status).toBe("paid");
      const after = await payouts(asApprover, run.id);
      const pa = after.find((p) => p.id === a.id) as Payout;
      const pb = after.find((p) => p.id === b.id) as Payout;
      expect(pa.current_attempt).toBe(1);
      expect(pa.attempts.map((x) => x.status)).toEqual(["settled"]);
      expect(pb.attempts.map((x) => [x.attempt, x.status, x.failure_reason])).toEqual([
        [1, "rejected", "operator_rejected"],
        [2, "settled", null],
      ]);
    });
  });
});
