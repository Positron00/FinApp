import { uuidv7 } from "@/lib/ids";
import { formatBps, formatCents } from "@/lib/money";
import { failureReasonLabel, payoutBadge } from "@/lib/payout-badge";
import { hasAnyRole } from "@/lib/roles";

import { attempt, payout } from "../helpers/fixtures";

describe("formatCents (ADR 0006)", () => {
  // ADR 0006: integer arithmetic; null shows "—".
  it.each([
    [0, "$0.00"],
    [5, "$0.05"],
    [123456, "$1,234.56"],
    [-1050, "-$10.50"],
    [100_000_000_000, "$1,000,000,000.00"],
    [null, "—"],
  ])("formats %s", (cents, want) => {
    expect(formatCents(cents)).toBe(want);
  });

  // ADR 0006: no floats.
  it("refuses non-integers", () => {
    expect(() => formatCents(1.5)).toThrow();
  });

  it("formats basis points", () => {
    expect(formatBps(2000)).toBe("20.00%");
    expect(formatBps(505)).toBe("5.05%");
    expect(formatBps(null)).toBe("—");
  });
});

describe("payoutBadge (§6.3)", () => {
  // PAY-413, PAY-501: every row of the badge table.
  it("maps each row", () => {
    expect(
      payoutBadge(
        payout({ status: "paid", no_transfer: true, current_attempt: 0, attempts: [] }) as never,
      ).label,
    ).toBe("Paid (no transfer)");
    expect(payoutBadge(payout() as never).label).toBe("Paid");
    expect(
      payoutBadge(
        payout({
          status: "failed",
          attempts: [attempt({ status: "rejected", failure_reason: "account_closed" })],
        }) as never,
      ),
    ).toMatchObject({ kind: "failed", label: "Failed", reason: "Account closed" });
    expect(
      payoutBadge(
        payout({ status: "requested", attempts: [attempt({ status: "requested" })] }) as never,
      ).label,
    ).toBe("Processing");
    expect(
      payoutBadge(
        payout({ status: "requested", attempts: [attempt({ status: "indeterminate" })] }) as never,
      ).label,
    ).toBe("Awaiting bank");
    expect(
      payoutBadge(
        payout({ status: "requested", attempts: [attempt({ status: "needs_operator" })] }) as never,
      ),
    ).toMatchObject({
      kind: "blocked",
      label: "Blocked",
      tooltip: "An operator must resolve this payout. It can't be retried.",
    });
  });

  it("labels failure reasons", () => {
    expect(failureReasonLabel("insufficient_funds_at_bank")).toBe("Insufficient funds at the bank");
    expect(failureReasonLabel("operator_rejected")).toBe("Rejected by an operator");
    expect(failureReasonLabel("key_window_expired")).toBe("Bank key expired"); // C73
    expect(failureReasonLabel("weird_code")).toBe("weird_code");
    expect(failureReasonLabel(null)).toBeUndefined();
  });
});

describe("shared helpers", () => {
  // PAY-601: any one of the required roles suffices.
  it("hasAnyRole", () => {
    expect(hasAnyRole(["approver"], ["payroll_admin", "approver"])).toBe(true);
    expect(hasAnyRole(["admin"], ["approver"])).toBe(false);
  });

  // PAY-604, NFR-09: request IDs are UUIDv7.
  it("uuidv7 has version 7, the variant bits and a time prefix", () => {
    const id = uuidv7(0x0193a0000000);
    expect(id).toMatch(/^0193a000-0000-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(uuidv7()).not.toBe(uuidv7());
  });
});
