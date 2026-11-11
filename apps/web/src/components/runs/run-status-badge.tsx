import type { BadgeKind } from "@/lib/payout-badge";
import type { PayRunStatus } from "@/lib/schemas/responses";

/** Status badges carry text and an icon, never colour alone (PAY-NFR-14). */
const RUN: Record<PayRunStatus, { label: string; icon: string; cls: string }> = {
  draft: { label: "Draft", icon: "✎", cls: "border-gray-500 text-ink" },
  calculated: { label: "Calculated", icon: "∑", cls: "border-brand text-brand" },
  processing: { label: "Processing", icon: "⟳", cls: "border-warn text-warn" },
  paid: { label: "Paid", icon: "✓", cls: "border-ok text-ok" },
  partially_failed: { label: "Partially failed", icon: "!", cls: "border-danger text-danger" },
};

export function RunStatusBadge({ status }: { status: PayRunStatus }) {
  const b = RUN[status];
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-2 text-sm ${b.cls}`}>
      <span aria-hidden="true">{b.icon}</span>
      {b.label}
    </span>
  );
}

const PAYOUT: Record<BadgeKind, { icon: string; cls: string }> = {
  paid: { icon: "✓", cls: "border-ok text-ok" },
  paid_no_transfer: { icon: "✓", cls: "border-ok text-ok" },
  failed: { icon: "✕", cls: "border-danger text-danger" },
  processing: { icon: "⟳", cls: "border-gray-500 text-ink" },
  awaiting_bank: { icon: "…", cls: "border-warn text-warn" },
  blocked: { icon: "!", cls: "border-danger text-danger" },
};

export function PayoutStatusBadge({
  kind,
  label,
  tooltip,
}: {
  kind: BadgeKind;
  label: string;
  tooltip?: string;
}) {
  const b = PAYOUT[kind];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-2 text-sm ${b.cls}`}
      title={tooltip}
    >
      <span aria-hidden="true">{b.icon}</span>
      {label}
    </span>
  );
}
