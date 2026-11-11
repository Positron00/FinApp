"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { ErrorBanner } from "@/components/error-banner";
import { FormError } from "@/components/field";
import { ApiError, apiFetch } from "@/lib/client/api";
import { errorEmployeeIds, errorText } from "@/lib/client/form-errors";
import { listQuery, queryKeys, type PageOf } from "@/lib/client/query-keys";
import { formatCents, hoursToInput, parseHours } from "@/lib/money";
import type { Hours, PayRun, Payslip } from "@/lib/schemas/responses";

/** Entries per PUT (PAY-301: 1–500 per request); more go in sequential requests. */
export const HOURS_BATCH = 500;
/** The schema's upper bound; payroll applies the per-schedule one (33,600 biweekly). */
const MAX_HUNDREDTHS = 74_400;

/**
 * Every payslip of a run, following `next_cursor` with `limit=100` until it's null (C8). The
 * editor needs them all before it can show a complete hours list.
 */
export async function fetchAllPayslips(runId: string): Promise<Payslip[]> {
  const out: Payslip[] = [];
  let cursor: string | null = null;
  do {
    const page: PageOf<Payslip> = await apiFetch<PageOf<Payslip>>(
      `/api/pay-runs/${runId}/payslips?${listQuery(100, cursor)}`,
    );
    out.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor !== null);
  return out;
}

export function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

/**
 * The hours editor (web LLD §6.3; PAY-301, C7, C8): payroll_admin, on `draft` or `calculated`
 * runs. It lists the hourly payslips, each with a labelled input prefilled from
 * `hours_hundredths`, and saves the changed rows (batches of 500). Saving a calculated run moves
 * it back to draft, which the editor says first. 422 `not_hourly` / `employee_not_in_run`, and
 * calculate's `hours_missing` (passed in as `highlight`), mark the rows in `details.employee_ids`.
 */
export function HoursEditor({
  run,
  highlight = [],
  disabled,
}: {
  run: PayRun;
  highlight?: string[];
  disabled?: boolean;
}) {
  const qc = useQueryClient();
  const id = useId();
  const all = useQuery({
    queryKey: queryKeys.allPayslips(run.id),
    queryFn: () => fetchAllPayslips(run.id),
  });
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [invalid, setInvalid] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: async (entries: { employee_id: string; hours_hundredths: number }[]) => {
      let last: Hours | undefined;
      for (const batch of chunk(entries, HOURS_BATCH)) {
        last = await apiFetch<Hours>(`/api/pay-runs/${run.id}/hours`, {
          method: "PUT",
          body: { entries: batch },
        });
      }
      return last;
    },
    retry: 0,
    onSuccess: () => {
      setEdits({});
      setSaved(true);
      // C7: the run is back in draft; refetch it and its payslips.
      void qc.invalidateQueries({ queryKey: queryKeys.payRun(run.id) });
    },
  });

  if (all.isError) return <ErrorBanner error={all.error} onRetry={() => void all.refetch()} />;
  if (!all.data) return <p className="mt-3">Loading hours…</p>;
  const hourly = all.data.filter((p) => p.pay_type === "hourly");
  if (hourly.length === 0) return null;

  const serverIds = new Set([...errorEmployeeIds(save.error), ...highlight]);
  const valueOf = (p: Payslip) => edits[p.employee_id] ?? hoursToInput(p.hours_hundredths);

  function onSave() {
    setSaved(false);
    const bad = new Set<string>();
    const entries: { employee_id: string; hours_hundredths: number }[] = [];
    for (const p of hourly) {
      const text = edits[p.employee_id];
      if (text === undefined || text === hoursToInput(p.hours_hundredths)) continue;
      const h = parseHours(text);
      if (h === null || h > MAX_HUNDREDTHS) bad.add(p.employee_id);
      else if (h !== p.hours_hundredths) {
        entries.push({ employee_id: p.employee_id, hours_hundredths: h });
      }
    }
    setInvalid(bad);
    if (bad.size > 0) {
      const first = hourly.find((p) => bad.has(p.employee_id));
      if (first) document.getElementById(`${id}-${first.employee_id}`)?.focus();
      return;
    }
    if (entries.length > 0) save.mutate(entries);
  }

  const err = save.error;
  return (
    <section aria-labelledby={`${id}-title`} className="mt-4 rounded border p-3">
      <h2 id={`${id}-title`} className="text-lg font-semibold">
        Hours
      </h2>
      {run.status === "calculated" ? (
        <p className="text-sm">
          Saving hours moves this run back to draft. Calculate it again before approving.
        </p>
      ) : null}
      <table className="mt-2 w-full text-left">
        <caption className="sr-only">Hours for hourly employees</caption>
        <thead>
          <tr>
            <th scope="col">Employee</th>
            <th scope="col">Rate</th>
            <th scope="col">Hours</th>
          </tr>
        </thead>
        <tbody>
          {hourly.map((p) => {
            const rowBad = invalid.has(p.employee_id) || serverIds.has(p.employee_id);
            return (
              <tr key={p.id} className={`border-t ${rowBad ? "bg-red-50" : ""}`}>
                <td>{p.employee_name}</td>
                <td>{formatCents(p.hourly_rate_cents)} / hour</td>
                <td>
                  <input
                    id={`${id}-${p.employee_id}`}
                    className="w-28 rounded border border-gray-400 p-1"
                    inputMode="decimal"
                    aria-label={`Hours for ${p.employee_name}`}
                    aria-invalid={rowBad ? true : undefined}
                    aria-describedby={rowBad ? `${id}-${p.employee_id}-err` : undefined}
                    value={valueOf(p)}
                    disabled={disabled}
                    onChange={(e) => {
                      setSaved(false);
                      setEdits((old) => ({ ...old, [p.employee_id]: e.target.value }));
                    }}
                  />
                  {rowBad ? (
                    <span id={`${id}-${p.employee_id}-err`} className="ml-2 text-danger">
                      {invalid.has(p.employee_id)
                        ? "Enter hours like 80 or 80.5 (up to 744)."
                        : "Check this row."}
                    </span>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <FormError
        message={err ? errorText(err) : null}
        requestId={err instanceof ApiError ? err.requestId : null}
      />
      {saved ? <p role="status">Hours saved. The run is in draft until you calculate it.</p> : null}
      <button
        type="button"
        className="mt-2 rounded border border-brand px-4 py-2 text-brand disabled:opacity-50"
        disabled={disabled || save.isPending || Object.keys(edits).length === 0}
        onClick={onSave}
      >
        {save.isPending ? "Saving…" : "Save hours"}
      </button>
    </section>
  );
}
