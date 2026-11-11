"use client";

import { useQuery } from "@tanstack/react-query";

import { Dialog } from "@/components/dialog";
import { ErrorBanner } from "@/components/error-banner";
import { apiFetch } from "@/lib/client/api";
import { queryKeys } from "@/lib/client/query-keys";
import { formatCents, formatHours } from "@/lib/money";
import type { Payslip } from "@/lib/schemas/responses";

/** One payslip (web LLD §6.3, PAY-303): `GET …/payslips/[payslipId]`; amounts "—" while pending. */
export function PayslipDialog({
  runId,
  payslipId,
  onClose,
}: {
  runId: string;
  payslipId: string;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: queryKeys.payslip(runId, payslipId),
    queryFn: () => apiFetch<Payslip>(`/api/pay-runs/${runId}/payslips/${payslipId}`),
  });
  const p = q.data;
  return (
    <Dialog title={p ? `Payslip: ${p.employee_name}` : "Payslip"} onClose={onClose}>
      {q.isError ? <ErrorBanner error={q.error} onRetry={() => void q.refetch()} /> : null}
      {!p && !q.isError ? <p className="mt-3">Loading…</p> : null}
      {p ? (
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1">
          <dt className="text-muted">Pay type</dt>
          <dd>{p.pay_type === "salaried" ? "Salaried" : "Hourly"}</dd>
          {p.pay_type === "salaried" ? (
            <>
              <dt className="text-muted">Annual salary</dt>
              <dd>{formatCents(p.annual_salary_cents)}</dd>
            </>
          ) : (
            <>
              <dt className="text-muted">Hourly rate</dt>
              <dd>{formatCents(p.hourly_rate_cents)}</dd>
              <dt className="text-muted">Hours</dt>
              <dd>{formatHours(p.hours_hundredths)}</dd>
            </>
          )}
          <dt className="text-muted">Status</dt>
          <dd>{p.status === "calculated" ? "Calculated" : "Pending"}</dd>
          <dt className="text-muted">Gross</dt>
          <dd>{formatCents(p.gross_cents)}</dd>
          <dt className="text-muted">Tax</dt>
          <dd>{formatCents(p.tax_cents)}</dd>
          <dt className="text-muted">Retirement</dt>
          <dd>{formatCents(p.retirement_cents)}</dd>
          <dt className="text-muted">Net</dt>
          <dd>{formatCents(p.net_cents)}</dd>
        </dl>
      ) : null}
      <button type="button" className="mt-4 rounded border px-4 py-2" onClick={onClose}>
        Close
      </button>
    </Dialog>
  );
}
