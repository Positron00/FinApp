"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { ApiError, apiFetch } from "@/lib/client/api";
import { queryKeys } from "@/lib/client/query-keys";
import { formatCents } from "@/lib/money";
import type { PayRun } from "@/lib/schemas/responses";

/**
 * Approve (web LLD §3.2 "Approve idempotency", PAY-400, PAY-401): one `crypto.randomUUID()` per
 * click, kept in the mutation's variables; disabled while pending; never retried. On a network
 * error the run is refetched: `processing` or `paid` means the first request landed.
 */
export function ApproveButton({
  run,
  meId,
  disabled,
}: {
  run: PayRun;
  meId: string;
  disabled?: boolean;
}) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [landed, setLanded] = useState(false);
  const errorId = useId();

  const approve = useMutation({
    mutationFn: (idempotencyKey: string) =>
      apiFetch<PayRun>(`/api/pay-runs/${run.id}/approve`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
      }),
    retry: 0,
    onSuccess: (updated) => qc.setQueryData(queryKeys.payRun(run.id), updated),
    onError: async (err) => {
      if (err instanceof ApiError) return;
      // No response: did the approval land anyway?
      try {
        const fresh = await apiFetch<PayRun>(`/api/pay-runs/${run.id}`);
        qc.setQueryData(queryKeys.payRun(run.id), fresh);
        if (fresh.status === "processing" || fresh.status === "paid") setLanded(true);
      } catch {
        // still offline: the error stays and the user can click again
      }
    },
  });

  const err = approve.error;
  const message =
    landed || !err
      ? null
      : err instanceof ApiError
        ? err.message
        : "The approval couldn't be confirmed. Check your connection and try again.";

  return (
    <div className="flex flex-col gap-2">
      {run.require_dual_approval && run.calculated_by === meId ? (
        <p className="text-sm text-muted">
          You calculated this run. With dual approval on, another approver must approve it.
        </p>
      ) : null}
      {!confirming ? (
        <button
          type="button"
          className="w-fit rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
          disabled={disabled || approve.isPending}
          aria-describedby={message ? errorId : undefined}
          onClick={() => setConfirming(true)}
        >
          Approve
        </button>
      ) : (
        <div role="group" aria-label="Confirm approval" className="rounded border p-3">
          <p>
            Approve paying {run.employee_count} employee(s), funding{" "}
            {formatCents(run.totals?.funding_required_cents)}?
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
              disabled={disabled || approve.isPending}
              onClick={() => {
                setLanded(false);
                approve.mutate(crypto.randomUUID());
                setConfirming(false);
              }}
            >
              Confirm approval
            </button>
            <button
              type="button"
              className="rounded border px-4 py-2"
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {landed ? <p role="status">Approved. Payouts are on their way.</p> : null}
      {message ? (
        <p id={errorId} role="alert" className="text-danger">
          {message}
          {err instanceof ApiError && err.requestId ? ` Reference: ${err.requestId}.` : ""}
        </p>
      ) : null}
    </div>
  );
}
