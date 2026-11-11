"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { CursorPager, useCursorStack } from "@/components/cursor-pager";
import { ErrorBanner } from "@/components/error-banner";
import { apiFetch } from "@/lib/client/api";
import { queryKeys } from "@/lib/client/query-keys";
import { formatCents } from "@/lib/money";
import type { PayRun } from "@/lib/schemas/responses";

import { RunStatusBadge } from "./run-status-badge";

interface PageOf<T> {
  items: T[];
  next_cursor: string | null;
}

/** The run list (web LLD §6.2 `/pay-runs`, PAY-303): newest period first, cursor-paged (C8). */
export function RunList({ canCreate }: { canCreate: boolean }) {
  const pager = useCursorStack();
  const q = useQuery({
    queryKey: queryKeys.payRuns(pager.cursor),
    queryFn: () => {
      const qs = new URLSearchParams({ limit: "50" });
      if (pager.cursor) qs.set("cursor", pager.cursor);
      return apiFetch<PageOf<PayRun>>(`/api/pay-runs?${qs.toString()}`);
    },
  });

  return (
    <>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Pay runs</h1>
        {canCreate ? (
          <Link className="rounded bg-brand px-4 py-2 text-white" href="/pay-runs/new">
            New pay run
          </Link>
        ) : null}
      </div>
      {q.isError ? <ErrorBanner error={q.error} onRetry={() => void q.refetch()} /> : null}
      {q.isPending ? <p className="mt-4">Loading…</p> : null}
      {q.data ? (
        <>
          <table className="mt-4 w-full text-left">
            <caption className="sr-only">Pay runs, newest period first</caption>
            <thead>
              <tr>
                <th scope="col">Period</th>
                <th scope="col">Schedule</th>
                <th scope="col">Pay date</th>
                <th scope="col">Status</th>
                <th scope="col">Net total</th>
                <th scope="col">Paid / failed / requested</th>
              </tr>
            </thead>
            <tbody>
              {q.data.items.map((r) => (
                <tr key={r.id} className="border-t">
                  <td>
                    <Link className="text-brand underline" href={`/pay-runs/${r.id}`}>
                      {r.period_start} – {r.period_end}
                    </Link>
                  </td>
                  <td>{r.schedule}</td>
                  <td>{r.pay_date}</td>
                  <td>
                    <RunStatusBadge status={r.status} />
                  </td>
                  <td>{formatCents(r.totals?.net_cents)}</td>
                  <td>
                    {r.counts.paid} / {r.counts.failed} / {r.counts.requested}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {q.data.items.length === 0 ? <p className="mt-4">No pay runs yet.</p> : null}
          <CursorPager
            nextCursor={q.data.next_cursor}
            hasPrevious={pager.hasPrevious}
            onNext={pager.next}
            onPrevious={pager.previous}
          />
        </>
      ) : null}
    </>
  );
}
