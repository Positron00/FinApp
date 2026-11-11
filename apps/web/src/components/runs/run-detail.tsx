"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { CursorPager, useCursorStack } from "@/components/cursor-pager";
import { ConfirmDialog } from "@/components/dialog";
import { ErrorBanner } from "@/components/error-banner";
import { LiveRegion } from "@/components/live-region";
import { TabList, TabPanel } from "@/components/tabs";
import { ApiError, apiFetch } from "@/lib/client/api";
import { errorEmployeeIds, errorText } from "@/lib/client/form-errors";
import { listQuery, queryKeys, type PageOf } from "@/lib/client/query-keys";
import { formatBps, formatCents, formatHours } from "@/lib/money";
import type { Role } from "@/lib/roles";
import type { PayRun, PayRunStatus, Payslip } from "@/lib/schemas/responses";

import { ApproveButton } from "./approve-button";
import { HoursEditor } from "./hours-editor";
import { PayoutsTab, type PayoutFilter } from "./payouts-tab";
import { PayslipDialog } from "./payslip-dialog";
import { RunStatusBadge } from "./run-status-badge";

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
 * The run page (web LLD §6.3; PAY-301–304, PAY-400, PAY-413, PAY-500–502). The run query polls
 * every 2 s while `processing` and stops at `paid`/`partially_failed`; the payouts query polls
 * only while its tab is open. A failed refetch keeps the last snapshot, marks it, and disables
 * the actions (calculate, delete, hours, approve, retry, resolve).
 */
export function RunDetail({ runId, me }: { runId: string; me: { id: string; roles: Role[] } }) {
  const qc = useQueryClient();
  const router = useRouter();
  const run = useQuery({
    queryKey: queryKeys.payRun(runId),
    queryFn: () => apiFetch<PayRun>(`/api/pay-runs/${runId}`),
    refetchInterval: (q) => (q.state.data?.status === "processing" ? POLL_MS : false),
    refetchIntervalInBackground: false,
  });
  const status = run.data?.status;
  const [tab, setTab] = useState<Tab | null>(null);
  const [payoutFilter, setPayoutFilter] = useState<PayoutFilter>("");
  const [payslipOpen, setPayslipOpen] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [notice, setNotice] = useState("");
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

  const remove = useMutation({
    mutationFn: () => apiFetch<void>(`/api/pay-runs/${runId}`, { method: "DELETE" }),
    retry: 0,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["pay-runs"] });
      router.push("/pay-runs");
    },
  });

  if (run.isPending) return <p>Loading…</p>;
  if (!run.data) return <ErrorBanner error={run.error} onRetry={() => void run.refetch()} />;

  const r = run.data;
  const stale = run.isError;
  const canPrepare = me.roles.includes("payroll_admin");
  const canApprove = me.roles.includes("approver");
  const editable = r.status === "draft" || r.status === "calculated";
  const c = r.counts;
  const missing = errorEmployeeIds(calculate.error);

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
            <dd>
              {who(r.calculated_by, r.calculated_by_name, me.id)}
              {r.calculated_at ? `, ${new Date(r.calculated_at).toLocaleString()}` : ""}
            </dd>
          </>
        ) : null}
        {r.approved_by ? (
          <>
            <dt className="text-muted">Approved by</dt>
            <dd>
              {who(r.approved_by, r.approved_by_name, me.id)}
              {r.approved_at ? `, ${new Date(r.approved_at).toLocaleString()}` : ""}
            </dd>
          </>
        ) : null}
      </dl>

      {stale ? (
        <p role="status" className="mt-3 text-warn">
          Last updated {new Date(run.dataUpdatedAt).toLocaleTimeString()}.{" "}
          {run.error instanceof ApiError && run.error.status === 429
            ? "Live updates paused."
            : "Updates paused."}
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
              <button
                type="button"
                className="underline"
                onClick={() => {
                  setPayoutFilter("requested");
                  setTab("payouts");
                }}
              >
                Show payouts
              </button>
            </p>
          ) : null}
        </div>
      ) : null}

      {notice ? (
        <p role="status" className="mt-3">
          {notice}
        </p>
      ) : null}

      <section aria-label="Actions" className="mt-4 flex flex-col gap-3">
        {canPrepare && editable ? (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="rounded border border-brand px-4 py-2 text-brand disabled:opacity-50"
              disabled={stale || calculate.isPending}
              aria-describedby={calculate.error ? "calculate-error" : undefined}
              onClick={() => calculate.mutate()}
            >
              {calculate.isPending ? "Calculating…" : "Calculate"}
            </button>
            <button
              type="button"
              className="rounded border border-danger px-4 py-2 text-danger disabled:opacity-50"
              disabled={stale || remove.isPending}
              onClick={() => setConfirmDelete(true)}
            >
              Delete run
            </button>
          </div>
        ) : null}
        {calculate.error ? (
          <p id="calculate-error" role="alert" className="text-danger">
            {errorText(calculate.error)}
            {calculate.error instanceof ApiError && calculate.error.requestId
              ? ` Reference: ${calculate.error.requestId}.`
              : ""}
          </p>
        ) : null}
        {remove.error ? (
          <p role="alert" className="text-danger">
            {errorText(remove.error)}
          </p>
        ) : null}
        {canApprove && r.status === "calculated" ? (
          <ApproveButton run={r} meId={me.id} disabled={stale} />
        ) : null}
      </section>

      {canPrepare && editable ? <HoursEditor run={r} highlight={missing} disabled={stale} /> : null}

      <RunTabs active={activeTab} onSelect={setTab} />
      <TabPanel id="payslips" active={activeTab === "payslips"}>
        <PayslipsTable run={r} onOpen={setPayslipOpen} />
      </TabPanel>
      <TabPanel id="payouts" active={activeTab === "payouts"}>
        <PayoutsTab
          key={payoutFilter}
          runId={runId}
          pollMs={r.status === "processing" ? POLL_MS : false}
          canAct={canApprove}
          disabled={stale}
          initialFilter={payoutFilter}
          onOpenPayslip={setPayslipOpen}
          onNotice={setNotice}
        />
      </TabPanel>

      {payslipOpen ? (
        <PayslipDialog runId={runId} payslipId={payslipOpen} onClose={() => setPayslipOpen(null)} />
      ) : null}
      {confirmDelete ? (
        <ConfirmDialog
          title="Delete this pay run?"
          confirmLabel="Delete run"
          pending={remove.isPending}
          onClose={() => setConfirmDelete(false)}
          onConfirm={() => {
            setConfirmDelete(false);
            remove.mutate();
          }}
        >
          <p>
            The run for {r.period_start} – {r.period_end} and its payslips are removed. The period
            becomes free for a new run.
          </p>
        </ConfirmDialog>
      ) : null}
    </>
  );
}

type Tab = "payslips" | "payouts";
const TAB_LIST = [
  { id: "payslips", label: "Payslips" },
  { id: "payouts", label: "Payouts" },
] as const satisfies readonly { id: Tab; label: string }[];

/** The run's tabs: the WAI-ARIA tabs pattern from `components/tabs.tsx` (PAY-NFR-14). */
export function RunTabs({ active, onSelect }: { active: Tab; onSelect: (t: Tab) => void }) {
  return <TabList tabs={TAB_LIST} active={active} onSelect={onSelect} label="Run details" />;
}

function PayslipsTable({ run, onOpen }: { run: PayRun; onOpen: (id: string) => void }) {
  const pager = useCursorStack();
  const q = useQuery({
    queryKey: queryKeys.payslips(run.id, pager.cursor),
    queryFn: () =>
      apiFetch<PageOf<Payslip>>(`/api/pay-runs/${run.id}/payslips?${listQuery(50, pager.cursor)}`),
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
            <th scope="col">Hours</th>
            <th scope="col">Gross</th>
            <th scope="col">Tax</th>
            <th scope="col">Retirement</th>
            <th scope="col">Net</th>
          </tr>
        </thead>
        <tbody>
          {q.data.items.map((p) => (
            <tr key={p.id} className="border-t">
              <td>
                <button type="button" className="text-brand underline" onClick={() => onOpen(p.id)}>
                  {p.employee_name}
                </button>
              </td>
              <td>{p.pay_type}</td>
              <td>{p.pay_type === "hourly" ? formatHours(p.hours_hundredths) : ""}</td>
              <td>{formatCents(p.gross_cents)}</td>
              <td>{formatCents(p.tax_cents)}</td>
              <td>{formatCents(p.retirement_cents)}</td>
              <td>{formatCents(p.net_cents)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t font-semibold">
            <th scope="row" colSpan={3}>
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
