"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { SelectField, TextField } from "@/components/field";
import { ApiError, apiFetch } from "@/lib/client/api";
import { errorEmployeeIds } from "@/lib/client/form-errors";
import { queryKeys, type PageOf } from "@/lib/client/query-keys";
import { PayRunCreateBody } from "@/lib/schemas/requests";
import type { PayRun, Settings } from "@/lib/schemas/responses";

type Schedule = "monthly" | "biweekly";
const DAY_MS = 86_400_000;

/** A calendar date "YYYY-MM-DD" as UTC midnight milliseconds (dates only; no time zones). */
function dayMs(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number) as [number, number, number];
  return Date.UTC(y, m - 1, d);
}

function isoOf(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/** Today's calendar date in the browser, as "YYYY-MM-DD". */
function localIso(today: Date): string {
  return `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
}

/** The next `count` first-of-month dates from `today`, as YYYY-MM-DD (calendar dates only). */
export function nextMonthStarts(today: Date, count = 3): string[] {
  const out: string[] = [];
  let y = today.getFullYear();
  let m = today.getMonth(); // this month's 1st first: a run for the current month is the usual case
  for (let i = 0; i < count; i++) {
    out.push(`${y}-${String(m + 1).padStart(2, "0")}-01`);
    m += 1;
    if (m === 12) {
      m = 0;
      y += 1;
    }
  }
  return out;
}

/**
 * Biweekly period starts `anchor + 14k` (PAY-300): the period containing `today` first, then the
 * next ones.
 */
export function biweeklyStarts(anchor: string, today: Date, count = 3): string[] {
  const a = dayMs(anchor);
  const t = dayMs(localIso(today));
  const k = Math.floor((t - a) / (14 * DAY_MS));
  return Array.from({ length: count }, (_, i) => isoOf(a + (k + i) * 14 * DAY_MS));
}

/** For information only: payroll derives the real period end (PAY-300). */
export function derivedPeriodEnd(schedule: Schedule, start: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start)) return null;
  if (schedule === "biweekly") return isoOf(dayMs(start) + 13 * DAY_MS);
  const [y, m] = start.split("-").map(Number) as [number, number];
  return isoOf(Date.UTC(y, m, 0)); // day 0 of the next month = the last day of this one
}

/**
 * Create a run (web LLD §6.2 `/pay-runs/new`, PAY-300): either schedule; `period_start` with
 * suggestions (monthly: first-of-month dates; biweekly: `biweekly_anchor + 14k`) and the derived
 * period end for information. `pay_run_exists` links to the existing run when the list has it;
 * `employee_in_overlapping_run` lists the employee IDs payroll returned.
 */
export function RunCreateForm() {
  const router = useRouter();
  const [today] = useState(() => new Date());
  const [schedule, setSchedule] = useState<Schedule>("monthly");
  const [periodStart, setPeriodStart] = useState(() => nextMonthStarts(today)[0] ?? "");
  const [existing, setExisting] = useState<string | null>(null);
  const [inputError, setInputError] = useState<string | undefined>();
  const settings = useQuery({
    queryKey: queryKeys.settings,
    queryFn: () => apiFetch<Settings>("/api/settings"),
  });
  const anchor = settings.data?.biweekly_anchor;
  const suggestions =
    schedule === "monthly" ? nextMonthStarts(today) : anchor ? biweeklyStarts(anchor, today) : [];

  const create = useMutation({
    mutationFn: (body: PayRunCreateBody) =>
      apiFetch<PayRun>("/api/pay-runs", { method: "POST", body }),
    retry: 0,
    onSuccess: (run) => router.push(`/pay-runs/${run.id}`),
    onError: async (err, body) => {
      if (!(err instanceof ApiError) || err.code !== "pay_run_exists") return;
      try {
        const page = await apiFetch<PageOf<PayRun>>("/api/pay-runs?limit=100");
        const hit = page.items.find(
          (r) => r.schedule === body.schedule && r.period_start === body.period_start,
        );
        setExisting(hit?.id ?? null);
      } catch {
        // the message stands on its own without the link
      }
    },
  });

  function chooseSchedule(s: Schedule) {
    setSchedule(s);
    const first =
      s === "monthly" ? nextMonthStarts(today)[0] : anchor ? biweeklyStarts(anchor, today)[0] : "";
    setPeriodStart(first ?? "");
  }

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setExisting(null);
    const parsed = PayRunCreateBody.safeParse({ schedule, period_start: periodStart });
    if (!parsed.success) {
      setInputError("Enter a date as YYYY-MM-DD.");
      e.currentTarget.querySelector<HTMLInputElement>('[name="period_start"]')?.focus();
      return;
    }
    setInputError(undefined);
    create.mutate(parsed.data);
  }

  const err = create.error instanceof ApiError ? create.error : null;
  // LLD §3.3: the biweekly invalid_period_start names the anchor date; the BFF doesn't have it,
  // but this page does (GET /api/settings), so the date is filled in here.
  const errMessage =
    err?.code === "invalid_period_start" && create.variables?.schedule === "biweekly" && anchor
      ? `Biweekly runs must start on a period start date (every 14 days from ${anchor}).`
      : err?.message;
  // No response at all (network down): a generic message rather than silence (§6.1).
  const offline = create.error !== null && !err;
  const overlapIds = errorEmployeeIds(create.error);
  const end = derivedPeriodEnd(schedule, periodStart);
  return (
    <form className="mt-4 flex max-w-sm flex-col gap-4" onSubmit={onSubmit} noValidate>
      <SelectField
        label="Schedule"
        name="schedule"
        value={schedule}
        onChange={chooseSchedule}
        options={[
          { value: "monthly", label: "Monthly" },
          { value: "biweekly", label: "Biweekly" },
        ]}
      />
      <TextField
        label="Period start"
        name="period_start"
        type="date"
        value={periodStart}
        onChange={setPeriodStart}
        error={inputError}
        hint={
          schedule === "monthly"
            ? "Monthly runs start on the 1st."
            : anchor
              ? `Biweekly runs start every 14 days from ${anchor}.`
              : undefined
        }
      />
      {suggestions.length > 0 ? (
        <div role="group" aria-label="Suggested period starts" className="flex flex-wrap gap-2">
          {suggestions.map((d) => (
            <button
              key={d}
              type="button"
              className="rounded border px-2 py-1 text-sm"
              aria-pressed={d === periodStart}
              onClick={() => setPeriodStart(d)}
            >
              {d}
            </button>
          ))}
        </div>
      ) : null}
      {end ? <p className="text-sm">Period ends {end} (worked out by payroll).</p> : null}
      {err ? (
        <div role="alert" className="text-danger">
          <p>{errMessage}</p>
          {overlapIds.length > 0 ? (
            <ul className="list-disc pl-5 text-sm">
              {overlapIds.map((id) => (
                <li key={id}>
                  <Link className="underline" href={`/employees/${id}`}>
                    Employee {id.slice(0, 8)}
                  </Link>
                </li>
              ))}
            </ul>
          ) : null}
          {existing ? (
            <Link className="underline" href={`/pay-runs/${existing}`}>
              Open the existing run
            </Link>
          ) : null}
          {err.requestId ? <p className="text-sm">Reference: {err.requestId}</p> : null}
        </div>
      ) : null}
      {offline ? (
        <p role="alert" className="text-danger">
          The service is temporarily unavailable. Please try again.
        </p>
      ) : null}
      <button
        type="submit"
        className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
        disabled={create.isPending}
      >
        Create pay run
      </button>
    </form>
  );
}
