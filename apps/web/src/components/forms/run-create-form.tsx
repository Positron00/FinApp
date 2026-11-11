"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { ApiError, apiFetch } from "@/lib/client/api";
import { PayRunCreateBody } from "@/lib/schemas/requests";
import type { PayRun } from "@/lib/schemas/responses";

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
 * Create a run (web LLD §6.2 `/pay-runs/new`, PAY-300). The skeleton offers monthly runs; F5
 * adds the biweekly schedule with anchor-based suggestions.
 */
export function RunCreateForm() {
  const router = useRouter();
  const [suggestions] = useState(() => nextMonthStarts(new Date()));
  const [periodStart, setPeriodStart] = useState(suggestions[0] ?? "");
  const create = useMutation({
    mutationFn: (body: PayRunCreateBody) =>
      apiFetch<PayRun>("/api/pay-runs", { method: "POST", body }),
    onSuccess: (run) => router.push(`/pay-runs/${run.id}`),
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const parsed = PayRunCreateBody.safeParse({ schedule: "monthly", period_start: periodStart });
    if (parsed.success) create.mutate(parsed.data);
  }

  const err = create.error instanceof ApiError ? create.error : null;
  return (
    <form className="mt-4 flex max-w-sm flex-col gap-4" onSubmit={onSubmit}>
      <p>Schedule: monthly</p>
      <label className="flex flex-col gap-1">
        <span>Period start</span>
        <select
          className="rounded border border-gray-400 p-2"
          value={periodStart}
          onChange={(e) => setPeriodStart(e.target.value)}
        >
          {suggestions.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </label>
      {err ? (
        <div role="alert" className="text-danger">
          <p>{err.message}</p>
          {err.requestId ? <p className="text-sm">Reference: {err.requestId}</p> : null}
        </div>
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
