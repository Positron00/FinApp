"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { CursorPager, useCursorStack } from "@/components/cursor-pager";
import { ErrorBanner } from "@/components/error-banner";
import { SelectField } from "@/components/field";
import { apiFetch } from "@/lib/client/api";
import { listQuery, queryKeys, type PageOf } from "@/lib/client/query-keys";
import { formatCents } from "@/lib/money";
import { failureReasonLabel, payoutBadge } from "@/lib/payout-badge";
import type { Attempt, Payout } from "@/lib/schemas/responses";

import { ResolveDialog } from "./resolve-dialog";
import { RetryButton } from "./retry-button";
import { PayoutStatusBadge } from "./run-status-badge";

export type PayoutFilter = "" | "requested" | "paid" | "failed";

const ATTEMPT_LABELS: Record<Attempt["status"], string> = {
  requested: "Requested",
  indeterminate: "Awaiting bank",
  needs_operator: "Blocked",
  settled: "Settled",
  rejected: "Rejected",
};

/** Every attempt with its status, reason, bank reference, `••••last4` and times (PAY-501). */
export function AttemptHistory({ payout }: { payout: Payout }) {
  if (payout.attempts.length === 0) return <p className="text-sm">No transfer was needed.</p>;
  return (
    <table className="my-2 w-full text-left text-sm">
      <caption className="sr-only">Attempts for {payout.employee_name}</caption>
      <thead>
        <tr>
          <th scope="col">Attempt</th>
          <th scope="col">Status</th>
          <th scope="col">Reason</th>
          <th scope="col">Bank reference</th>
          <th scope="col">Account</th>
          <th scope="col">Requested</th>
          <th scope="col">Final</th>
        </tr>
      </thead>
      <tbody>
        {payout.attempts.map((a) => (
          <tr key={a.attempt}>
            <td>{a.attempt}</td>
            <td>{ATTEMPT_LABELS[a.status]}</td>
            <td>{failureReasonLabel(a.failure_reason) ?? "—"}</td>
            <td>{a.bank_reference ?? "—"}</td>
            <td>••••{a.account_last4}</td>
            <td>{new Date(a.requested_at).toLocaleString()}</td>
            <td>{a.final_at ? new Date(a.final_at).toLocaleString() : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The Payouts tab (web LLD §6.3; PAY-413, PAY-500, PAY-501, PAY-502): a status filter and a
 * cursor pager (C8); per row the amount, the current attempt's `••••last4`, the badge
 * ("Awaiting bank", "Blocked", …), the attempt number and a History disclosure. Approvers get
 * Retry on `failed` rows and Resolve on Blocked rows. The visible page polls every 2 s while the
 * run is `processing`.
 */
export function PayoutsTab({
  runId,
  pollMs,
  canAct,
  disabled,
  initialFilter = "",
  onOpenPayslip,
  onNotice,
}: {
  runId: string;
  pollMs: number | false;
  canAct: boolean;
  disabled?: boolean;
  initialFilter?: PayoutFilter;
  onOpenPayslip: (payslipId: string) => void;
  onNotice: (message: string) => void;
}) {
  const pager = useCursorStack();
  const [filter, setFilter] = useState<PayoutFilter>(initialFilter);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [resolving, setResolving] = useState<Payout | null>(null);
  const q = useQuery({
    queryKey: queryKeys.payouts(runId, pager.cursor, filter),
    queryFn: () =>
      apiFetch<PageOf<Payout>>(
        `/api/pay-runs/${runId}/payouts?${listQuery(50, pager.cursor, filter || undefined)}`,
      ),
    refetchInterval: pollMs,
    refetchIntervalInBackground: false,
  });

  function toggle(id: string) {
    setOpen((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <>
      <div className="mt-3 max-w-xs">
        <SelectField
          label="Show"
          name="payout_status"
          value={filter}
          onChange={(f) => {
            setFilter(f);
            pager.reset(); // a new filter starts from the first page (C8)
          }}
          options={[
            { value: "", label: "All payouts" },
            { value: "requested", label: "Requested" },
            { value: "paid", label: "Paid" },
            { value: "failed", label: "Failed" },
          ]}
        />
      </div>
      {q.isError && !q.data ? (
        <ErrorBanner error={q.error} onRetry={() => void q.refetch()} />
      ) : null}
      {!q.data && !q.isError ? <p>Loading…</p> : null}
      {q.data ? (
        <>
          <table className="mt-3 w-full text-left">
            <caption className="sr-only">Payouts</caption>
            <thead>
              <tr>
                <th scope="col">Employee</th>
                <th scope="col">Amount</th>
                <th scope="col">Bank</th>
                <th scope="col">Status</th>
                <th scope="col">Attempt</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            {q.data.items.map((p) => {
              const badge = payoutBadge(p);
              const current = p.attempts.find((a) => a.attempt === p.current_attempt);
              const expanded = open.has(p.id);
              return (
                <tbody key={p.id} className="border-t">
                  <tr>
                    <td>
                      <button
                        type="button"
                        className="text-brand underline"
                        onClick={() => onOpenPayslip(p.payslip_id)}
                      >
                        {p.employee_name}
                      </button>
                    </td>
                    <td>{formatCents(p.amount_cents)}</td>
                    <td>{current ? `••••${current.account_last4}` : "—"}</td>
                    <td>
                      <PayoutStatusBadge
                        kind={badge.kind}
                        label={badge.label}
                        tooltip={badge.tooltip}
                      />
                      {badge.reason ? <span className="ml-2">{badge.reason}</span> : null}
                    </td>
                    <td>{p.current_attempt || "—"}</td>
                    <td className="flex flex-wrap items-start gap-2 py-1">
                      <button
                        type="button"
                        className="rounded border px-2 py-1"
                        aria-expanded={expanded}
                        aria-controls={`history-${p.id}`}
                        onClick={() => toggle(p.id)}
                      >
                        History
                      </button>
                      {canAct && p.status === "failed" ? (
                        <RetryButton payout={p} disabled={disabled} onDone={onNotice} />
                      ) : null}
                      {canAct && badge.kind === "blocked" ? (
                        <button
                          type="button"
                          className="rounded border border-warn px-2 py-1 disabled:opacity-50"
                          disabled={disabled}
                          aria-label={`Resolve payout to ${p.employee_name}`}
                          onClick={() => setResolving(p)}
                        >
                          Resolve
                        </button>
                      ) : null}
                    </td>
                  </tr>
                  {expanded ? (
                    <tr id={`history-${p.id}`}>
                      <td colSpan={6}>
                        <AttemptHistory payout={p} />
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              );
            })}
          </table>
          {q.data.items.length === 0 ? <p className="mt-3">No payouts.</p> : null}
          <CursorPager
            nextCursor={q.data.next_cursor}
            hasPrevious={pager.hasPrevious}
            onNext={pager.next}
            onPrevious={pager.previous}
          />
        </>
      ) : null}
      {resolving ? (
        <ResolveDialog
          payout={resolving}
          onClose={() => setResolving(null)}
          onResolved={(m) => {
            setResolving(null);
            onNotice(m);
          }}
        />
      ) : null}
    </>
  );
}
