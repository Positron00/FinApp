"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId } from "react";

import { ApiError, apiFetch } from "@/lib/client/api";
import { errorText } from "@/lib/client/form-errors";
import { queryKeys } from "@/lib/client/query-keys";
import type { Payout } from "@/lib/schemas/responses";

/**
 * Retry a failed payout (web LLD §6.3; PAY-500, C52): approver, `failed` rows only. Each click
 * sends a new `Idempotency-Key` (`crypto.randomUUID()`), kept in the mutation's variables; the
 * button is disabled while pending, and the mutation is never retried. A retry moves the run
 * back to `processing`, so the run query is invalidated and polling restarts. Errors (the retry
 * `approver_must_differ` message, the 409s) render in `role="alert"`, linked to the button.
 */
export function RetryButton({
  payout,
  disabled,
  onDone,
}: {
  payout: Payout;
  disabled?: boolean;
  onDone?: (message: string) => void;
}) {
  const qc = useQueryClient();
  const errorId = useId();
  const retry = useMutation({
    mutationFn: (idempotencyKey: string) =>
      apiFetch<Payout>(`/api/pay-runs/${payout.pay_run_id}/payouts/${payout.id}/retry`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
      }),
    retry: 0,
    onSuccess: () => {
      onDone?.(`Retry sent for ${payout.employee_name}.`);
      void qc.invalidateQueries({ queryKey: queryKeys.payRun(payout.pay_run_id) });
    },
  });
  const err = retry.error;
  return (
    <span className="inline-flex flex-col">
      <button
        type="button"
        className="w-fit rounded border border-brand px-2 py-1 text-brand disabled:opacity-50"
        disabled={disabled || retry.isPending}
        aria-label={`Retry payout to ${payout.employee_name}`}
        aria-describedby={err ? errorId : undefined}
        onClick={() => retry.mutate(crypto.randomUUID())}
      >
        {retry.isPending ? "Retrying…" : "Retry"}
      </button>
      {err ? (
        <span id={errorId} role="alert" className="text-sm text-danger">
          {errorText(err)}
          {err instanceof ApiError && err.requestId ? ` Reference: ${err.requestId}.` : ""}
        </span>
      ) : null}
    </span>
  );
}
