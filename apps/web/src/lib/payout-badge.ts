import type { Payout } from "./schemas/responses";

/** The payout badge (web LLD §6.3, PAY-413, PAY-501). Shared and pure. */
export type BadgeKind =
  "paid" | "paid_no_transfer" | "failed" | "processing" | "awaiting_bank" | "blocked";

export interface Badge {
  kind: BadgeKind;
  label: string;
  reason?: string;
  tooltip?: string;
}

const REASONS: Record<string, string> = {
  account_closed: "Account closed",
  insufficient_funds_at_bank: "Insufficient funds at the bank",
  operator_rejected: "Rejected by an operator",
};

export function failureReasonLabel(code: string | null): string | undefined {
  if (code === null) return undefined;
  return REASONS[code] ?? code;
}

export function payoutBadge(
  p: Pick<Payout, "status" | "no_transfer" | "current_attempt" | "attempts">,
): Badge {
  const current = p.attempts.find((a) => a.attempt === p.current_attempt);
  if (p.status === "paid") {
    return p.no_transfer
      ? { kind: "paid_no_transfer", label: "Paid (no transfer)" }
      : { kind: "paid", label: "Paid" };
  }
  if (p.status === "failed") {
    return {
      kind: "failed",
      label: "Failed",
      reason: failureReasonLabel(current?.failure_reason ?? null),
    };
  }
  if (current?.status === "indeterminate") return { kind: "awaiting_bank", label: "Awaiting bank" };
  if (current?.status === "needs_operator") {
    return {
      kind: "blocked",
      label: "Blocked",
      tooltip: "An operator must resolve this payout. It can't be retried.",
    };
  }
  return { kind: "processing", label: "Processing" };
}
