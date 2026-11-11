"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { CursorPager, useCursorStack } from "@/components/cursor-pager";
import { ErrorBanner } from "@/components/error-banner";
import { LiveRegion } from "@/components/live-region";
import { ApiError, apiFetch } from "@/lib/client/api";
import { queryKeys } from "@/lib/client/query-keys";
import { formatBps, formatCents } from "@/lib/money";
import { payoutBadge } from "@/lib/payout-badge";
import type { Role } from "@/lib/roles";
import type { PayRun, PayRunStatus, Payout, Payslip } from "@/lib/schemas/responses";

import { ApproveButton } from "./approve-button";
import { PayoutStatusBadge, RunStatusBadge } from "./run-status-badge";

interface PageOf<T> {
  items: T[];
  next_cursor: string | null;
}

export const POLL_MS = 2000;
const FINAL: PayRunStatus[] = ["paid", "partially_failed"];

/** A user reference: "you", else the name, else the first 8 characters of the ID (§6.1). */
export function who(id: string | null, name: string | null, meId: string): string {
  if (id === null) return "—";
  if (id === meId) return "you";
  return name ?? id.slice(0, 8);
}

/** What the live region says: changes only when the status or a count changes (PAY-NFR-14). */
export function announcement(run: PayRun): string {
  const c = run.counts;
  if (run.status === "processing") {
    return `Run processing: ${c.paid} paid, ${c.failed} failed, ${c.requested} in progress`;
  }
  if (FINAL.includes(run.status)) {
    return `Run ${run.status === "paid" ? "paid" : "partially failed"}: ${c.paid} paid, ${c.failed} failed`;
  }
  return `Run ${run.status}`;
}

/**
 * The run page (web LLD §6.3; PAY-303, PAY-400, PAY-413). The run query polls every 2 s while
 * `processing` and stops at `paid`/`partially_failed`; the payouts query polls only while its tab
 * is open. A failed refetch keeps the last snapshot, marks it, and disables the actions.
 */
export function RunDetail({ runId, me }: { runId: string; me: { id: string; roles: Role[] } }) {
  const qc = useQueryClient();
  const run = useQuery({
    queryKey: queryKeys.payRun(runId),
    queryFn: () => apiFetch<PayRun>(`/api/pay-runs/${runId}`),
    refetchInterval: (q) => (q.state.data?.status === "processing" ? POLL_MS : false),
    refetchIntervalInBackground: false,
  });
  const status = run.data?.status;
  const [tab, setTab] = useState<"payslips" | "payouts" | null>(null);
  const activeTab = tab ?? (run.data?.approved_at ? "payouts" : "payslips");

  // When polling stops at a final status, refetch the payouts once so the rows are complete.
  const prevStatus = useRef<PayRunStatus | undefined>(undefined);
  useEffect(() => {
    if (prevStatus.current === "processing" && status && FINAL.includes(status)) {
      void qc.invalidateQueries({ queryKey: ["pay-run", runId, "payouts"] });
    }
    prevStatus.current = status;
  }, [status, qc, runId]);

  const calculate = useMutation({
    mutationFn: () => apiFetch<PayRun>(`/api/pay-runs/${runId}/calculate`, { method: "POST" }),
    retry: 0,
    onSuccess: (r) => {
      qc.setQueryData(queryKeys.payRun(runId), r);
      void qc.invalidateQueries({ queryKey: ["pay-run", runId, "payslips"] });
    },
  });

  if (run.isPending) return <p>Loading…</p>;
  if (!run.data) return <ErrorBanner error={run.error} onRetry={() => void run.refetch()} />;

  const r = run.data;
  const stale = run.isError;
  const canPrepare = me.roles.includes("payroll_admin");
  const canApprove = me.roles.includes("approver");
  const c = r.counts;

  return (
    <>
      <LiveRegion message={announcement(r)} />
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold">
          Pay run {r.period_start} – {r.period_end}
        </h1>
        <RunStatusBadge status={r.status} />
        {r.require_dual_approval ? (
          <span className="rounded border px-2 text-sm">Dual approval</span>
        ) : null}
      </div>
      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-4">
        <dt className="text-muted">Schedule</dt>
        <dd>{r.schedule}</dd>
        <dt className="text-muted">Pay date</dt>
        <dd>{r.pay_date}</dd>
        <dt className="text-muted">Employees</dt>
        <dd>{r.employee_count}</dd>
        <dt className="text-muted">Tax / retirement</dt>
        <dd>
          {formatBps(r.tax_bps)} / {formatBps(r.retirement_bps)}
        </dd>
        <dt className="text-muted">Gross</dt>
        <dd>{formatCents(r.totals?.gross_cents)}</dd>
        <dt className="text-muted">Tax</dt>
        <dd>{formatCents(r.totals?.tax_cents)}</dd>
        <dt className="text-muted">Retirement</dt>
        <dd>{formatCents(r.totals?.retirement_cents)}</dd>
        <dt className="text-muted">Net</dt>
        <dd>{formatCents(r.totals?.net_cents)}</dd>
        <dt className="text-muted">Funding required</dt>
        <dd>{formatCents(r.totals?.funding_required_cents)}</dd>
        <dt className="text-muted">Payouts</dt>
        <dd>
          {c.paid} paid{c.no_transfer > 0 ? ` (${c.no_transfer} with no transfer)` : ""}, {c.failed}{" "}
          failed, {c.requested} requested ({c.awaiting_bank} awaiting bank, {c.blocked} blocked)
        </dd>
        {r.calculated_by ? (
          <>
            <dt className="text-muted">Calculated by</dt>
            <dd>{who(r.calculated_by, r.calculated_by_name, me.id)}</dd>
          </>
        ) : null}
        {r.approved_by ? (
          <>
            <dt className="text-muted">Approved by</dt>
            <dd>{who(r.approved_by, r.approved_by_name, me.id)}</dd>
          </>
        ) : null}
      </dl>

      {stale ? (
        <p role="status" className="mt-3 text-warn">
          Last updated {new Date(run.dataUpdatedAt).toLocaleTimeString()}. Updates paused.
        </p>
      ) : null}

      {r.status === "processing" ? (
        <div className="mt-3 rounded border border-warn p-3">
          <p>
            Paying employees: {c.paid} of {c.paid + c.failed + c.requested} paid
          </p>
          {c.awaiting_bank > 0 ? <p>{c.awaiting_bank} awaiting bank</p> : null}
          {c.blocked > 0 ? (
            <p>
              {c.blocked} blocked: an operator must resolve these.{" "}
              <button type="button" className="underline" onClick={() => setTab("payouts")}>
                Show payouts
              </button>
            </p>
          ) : null}
        </div>
      ) : null}

      <section aria-label="Actions" className="mt-4 flex flex-col gap-3">
        {canPrepare && (r.status === "draft" || r.status === "calculated") ? (
          <div>
            <button
              type="button"
              className="rounded border border-brand px-4 py-2 text-brand disabled:opacity-50"
              disabled={stale || calculate.isPending}
              onClick={() => calculate.mutate()}
            >
              {calculate.isPending ? "Calculating…" : "Calculate"}
            </button>
            {calculate.error ? (
              <p role="alert" className="text-danger">
                {calculate.error instanceof ApiError
                  ? calculate.error.message
                  : "The service is temporarily unavailable. Please try again."}
              </p>
            ) : null}
          </div>
        ) : null}
        {canApprove && r.status === "calculated" ? (
          <ApproveButton run={r} meId={me.id} disabled={stale} />
        ) : null}
      </section>

      <div role="tablist" aria-label="Run details" className="mt-6 flex gap-2 border-b">
        {(["payslips", "payouts"] as const).map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            id={`tab-${t}`}
            aria-selected={activeTab === t}
            aria-controls={`panel-${t}`}
            className={`px-3 py-2 ${activeTab === t ? "border-b-2 border-brand font-semibold" : ""}`}
            onClick={() => setTab(t)}
          >
            {t === "payslips" ? "Payslips" : "Payouts"}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${activeTab}`} aria-labelledby={`tab-${activeTab}`}>
        {activeTab === "payslips" ? (
          <PayslipsTable run={r} />
        ) : (
          <PayoutsTable runId={runId} polling={r.status === "processing"} />
        )}
      </div>
    </>
  );
}

function PayslipsTable({ run }: { run: PayRun }) {
  const pager = useCursorStack();
  const q = useQuery({
    queryKey: queryKeys.payslips(run.id, pager.cursor),
    queryFn: () => {
      const qs = new URLSearchParams({ limit: "50" });
      if (pager.cursor) qs.set("cursor", pager.cursor);
      return apiFetch<PageOf<Payslip>>(`/api/pay-runs/${run.id}/payslips?${qs.toString()}`);
    },
  });
  if (q.isError) return <ErrorBanner error={q.error} onRetry={() => void q.refetch()} />;
  if (!q.data) return <p>Loading…</p>;
  return (
    <>
      <table className="mt-3 w-full text-left">
        <caption className="sr-only">Payslips</caption>
        <thead>
          <tr>
            <th scope="col">Employee</th>
            <th scope="col">Pay type</th>
            <th scope="col">Gross</th>
            <th scope="col">Tax</th>
            <th scope="col">Retirement</th>
            <th scope="col">Net</th>
          </tr>
        </thead>
        <tbody>
          {q.data.items.map((p) => (
            <tr key={p.id} className="border-t">
              <td>{p.employee_name}</td>
              <td>{p.pay_type}</td>
              <td>{formatCents(p.gross_cents)}</td>
              <td>{formatCents(p.tax_cents)}</td>
              <td>{formatCents(p.retirement_cents)}</td>
              <td>{formatCents(p.net_cents)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t font-semibold">
            <th scope="row" colSpan={2}>
              Run totals
            </th>
            <td>{formatCents(run.totals?.gross_cents)}</td>
            <td>{formatCents(run.totals?.tax_cents)}</td>
            <td>{formatCents(run.totals?.retirement_cents)}</td>
            <td>{formatCents(run.totals?.net_cents)}</td>
          </tr>
        </tfoot>
      </table>
      <CursorPager
        nextCursor={q.data.next_cursor}
        hasPrevious={pager.hasPrevious}
        onNext={pager.next}
        onPrevious={pager.previous}
      />
    </>
  );
}

function PayoutsTable({ runId, polling }: { runId: string; polling: boolean }) {
  const pager = useCursorStack();
  const q = useQuery({
    queryKey: queryKeys.payouts(runId, pager.cursor),
    queryFn: () => {
      const qs = new URLSearchParams({ limit: "50" });
      if (pager.cursor) qs.set("cursor", pager.cursor);
      return apiFetch<PageOf<Payout>>(`/api/pay-runs/${runId}/payouts?${qs.toString()}`);
    },
    refetchInterval: polling ? POLL_MS : false,
    refetchIntervalInBackground: false,
  });
  if (q.isError && !q.data) return <ErrorBanner error={q.error} onRetry={() => void q.refetch()} />;
  if (!q.data) return <p>Loading…</p>;
  return (
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
          </tr>
        </thead>
        <tbody>
          {q.data.items.map((p) => {
            const badge = payoutBadge(p);
            const current = p.attempts.find((a) => a.attempt === p.current_attempt);
            return (
              <tr key={p.id} className="border-t">
                <td>{p.employee_name}</td>
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
              </tr>
            );
          })}
        </tbody>
      </table>
      <CursorPager
        nextCursor={q.data.next_cursor}
        hasPrevious={pager.hasPrevious}
        onNext={pager.next}
        onPrevious={pager.previous}
      />
    </>
  );
}
