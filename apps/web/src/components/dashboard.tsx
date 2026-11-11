"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useId } from "react";

import { ErrorBanner } from "@/components/error-banner";
import { RunStatusBadge } from "@/components/runs/run-status-badge";
import { apiFetch } from "@/lib/client/api";
import { queryKeys, type PageOf } from "@/lib/client/query-keys";
import type { Employee, PayRun } from "@/lib/schemas/responses";

function useRuns(status: string | undefined) {
  return useQuery({
    queryKey: [...queryKeys.payRuns(null, status), "dashboard"],
    queryFn: () =>
      apiFetch<PageOf<PayRun>>(`/api/pay-runs?limit=10${status ? `&status=${status}` : ""}`),
  });
}

/** "12", or "50+" when there are more pages (the dashboard reads one page of 50). */
export function employeeCount(page: PageOf<Employee>): string {
  return page.next_cursor === null ? String(page.items.length) : `${page.items.length}+`;
}

function RunCard({
  title,
  runs,
  note,
}: {
  title: string;
  runs: ReturnType<typeof useRuns>;
  note?: (r: PayRun) => string | null;
}) {
  // An ID without whitespace: aria-labelledby is a space-separated ID list (PAY-NFR-14).
  const headingId = useId();
  return (
    <section className="rounded border p-4" aria-labelledby={headingId}>
      <h2 id={headingId} className="font-semibold">
        {title}
      </h2>
      {runs.isError ? <ErrorBanner error={runs.error} onRetry={() => void runs.refetch()} /> : null}
      {runs.data && runs.data.items.length === 0 ? <p className="text-muted">None.</p> : null}
      <ul className="mt-2 flex flex-col gap-1">
        {runs.data?.items.map((r) => (
          <li key={r.id} className="flex flex-wrap items-center gap-2">
            <Link className="text-brand underline" href={`/pay-runs/${r.id}`}>
              {r.period_start} – {r.period_end} ({r.schedule})
            </Link>
            <RunStatusBadge status={r.status} />
            {note?.(r) ? <span className="text-sm">{note(r)}</span> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * `/dashboard` (web LLD §6.2; PAY-303, PAY-413): runs in `processing` (with the blocked count),
 * runs `partially_failed` ("needs attention"), the latest runs and the active employee count.
 * Quick actions for payroll_admin.
 */
export function Dashboard({ canPrepare }: { canPrepare: boolean }) {
  const processing = useRuns("processing");
  const failed = useRuns("partially_failed");
  const latest = useRuns(undefined);
  const employees = useQuery({
    queryKey: [...queryKeys.employees(null, "active"), "dashboard"],
    queryFn: () => apiFetch<PageOf<Employee>>("/api/employees?status=active&limit=50"),
  });

  return (
    <>
      <h1 className="text-2xl font-semibold">Dashboard</h1>
      {canPrepare ? (
        <div className="mt-4 flex gap-2">
          <Link className="rounded bg-brand px-4 py-2 text-white" href="/pay-runs/new">
            New pay run
          </Link>
          <Link className="rounded border px-4 py-2" href="/employees/new">
            Add employee
          </Link>
        </div>
      ) : null}
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <RunCard
          title="Paying now"
          runs={processing}
          note={(r) => (r.counts.blocked > 0 ? `${r.counts.blocked} blocked` : null)}
        />
        <RunCard title="Needs attention" runs={failed} note={() => "needs attention"} />
        <RunCard title="Latest pay runs" runs={latest} />
        <section className="rounded border p-4" aria-labelledby="card-employees">
          <h2 id="card-employees" className="font-semibold">
            Active employees
          </h2>
          {employees.isError ? (
            <ErrorBanner error={employees.error} onRetry={() => void employees.refetch()} />
          ) : null}
          {employees.data ? <p className="text-2xl">{employeeCount(employees.data)}</p> : null}
          <Link className="text-brand underline" href="/employees">
            All employees
          </Link>
        </section>
      </div>
    </>
  );
}
