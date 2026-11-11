"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";

import { Dialog } from "@/components/dialog";
import { ApiError, apiFetch } from "@/lib/client/api";
import { apiFieldErrors, errorText } from "@/lib/client/form-errors";
import { queryKeys } from "@/lib/client/query-keys";
import { formatCents } from "@/lib/money";
import type { ResolveBody } from "@/lib/schemas/requests";
import type { Payout } from "@/lib/schemas/responses";

export const NOTE_MAX = 500;
type Action = ResolveBody["action"];

/**
 * The resolve dialog (web LLD §6.3; PAY-502, R8, C48): for a Blocked payout (current attempt
 * `needs_operator`), approver only. The bank-check warning comes first and gets the focus; the
 * two choices have no default; the note (1–500 characters, only its length audited) is required.
 * Submit is disabled until both are present and while pending; never retried.
 *
 * 409 `payout_not_blocked` shows its message and refetches the payouts. 409 `resolve_too_early`
 * keeps the dialog open with a countdown from `Retry-After`, during which "Mark rejected" is
 * disabled; "Send again" has no wait. Focus returns to the Resolve button on close.
 */
export function ResolveDialog({
  payout,
  onClose,
  onResolved,
}: {
  payout: Payout;
  onClose: () => void;
  onResolved: (message: string) => void;
}) {
  const qc = useQueryClient();
  const id = useId();
  const warningRef = useRef<HTMLHeadingElement>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [note, setNote] = useState("");
  const [wait, setWait] = useState(0);
  const current = payout.attempts.find((a) => a.attempt === payout.current_attempt);

  useEffect(() => {
    if (wait <= 0) return;
    const t = setTimeout(() => setWait((w) => w - 1), 1000);
    return () => clearTimeout(t);
  }, [wait]);

  const resolve = useMutation({
    mutationFn: (body: ResolveBody) =>
      apiFetch<Payout>(`/api/pay-runs/${payout.pay_run_id}/payouts/${payout.id}/resolve`, {
        method: "POST",
        body,
      }),
    retry: 0,
    onSuccess: (_p, body) => {
      void qc.invalidateQueries({ queryKey: queryKeys.payRun(payout.pay_run_id) });
      onResolved(body.action === "redrive" ? "Payout resent" : "Payout marked rejected");
    },
    onError: (err) => {
      if (!(err instanceof ApiError)) return;
      if (err.code === "resolve_too_early") {
        setWait(err.retryAfter ?? 0);
        if (action === "mark_rejected") setAction(null);
      }
      if (err.code === "payout_not_blocked") {
        void qc.invalidateQueries({ queryKey: ["pay-run", payout.pay_run_id, "payouts"] });
      }
    },
  });

  const err = resolve.error;
  const fields = apiFieldErrors(err);
  const tooEarly = err instanceof ApiError && err.code === "resolve_too_early";
  const trimmed = note.trim();
  const canSubmit =
    action !== null &&
    trimmed.length >= 1 &&
    note.length <= NOTE_MAX &&
    !resolve.isPending &&
    !(action === "mark_rejected" && wait > 0);
  const alertText = err
    ? tooEarly
      ? wait > 0
        ? `This payout was blocked moments ago. Wait ${wait} seconds, check the bank again, then mark it rejected.`
        : "You can now check the bank again and mark it rejected."
      : err instanceof ApiError && err.code === "validation_error"
        ? null
        : errorText(err)
    : null;

  return (
    <Dialog title="Resolve blocked payout" onClose={onClose} alert initialFocus={warningRef}>
      <section className="mt-3 rounded border border-warn p-3" aria-labelledby={`${id}-warn`}>
        <h3 id={`${id}-warn`} ref={warningRef} tabIndex={-1} className="font-semibold">
          Check the bank first
        </h3>
        <p>
          Look up this transfer with the bank (runbook <code>PayoutNeedsOperator.md</code>:{" "}
          <code>GET /v1/transfers/{"{key}"}</code>) before you choose. Don&apos;t mark it rejected
          if the bank shows it settled.
        </p>
      </section>
      <dl className="mt-3 grid grid-cols-2 gap-x-4">
        <dt className="text-muted">Employee</dt>
        <dd>{payout.employee_name}</dd>
        <dt className="text-muted">Amount</dt>
        <dd>{formatCents(payout.amount_cents)}</dd>
        <dt className="text-muted">Attempt</dt>
        <dd>{payout.current_attempt}</dd>
        <dt className="text-muted">Bank account</dt>
        <dd>{current ? `••••${current.account_last4}` : "—"}</dd>
      </dl>
      <form
        className="mt-3 flex flex-col gap-3"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit && action) resolve.mutate({ action, note: trimmed });
        }}
      >
        <fieldset
          className="flex flex-col gap-2"
          aria-invalid={fields.action ? true : undefined}
          aria-describedby={fields.action ? `${id}-action-err` : undefined}
        >
          <legend className="font-semibold">What should happen?</legend>
          <label className="flex gap-2">
            <input
              type="radio"
              name="action"
              value="redrive"
              checked={action === "redrive"}
              onChange={() => setAction("redrive")}
              aria-describedby={`${id}-redrive`}
            />
            <span>
              Send again (redrive)
              <span id={`${id}-redrive`} className="block text-sm text-muted">
                Use when the bank has no record of the transfer, or it is pending, and the cause is
                fixed. The same attempt is sent again.
              </span>
            </span>
          </label>
          <label className="flex gap-2">
            <input
              type="radio"
              name="action"
              value="mark_rejected"
              checked={action === "mark_rejected"}
              disabled={wait > 0}
              onChange={() => setAction("mark_rejected")}
              aria-describedby={`${id}-reject`}
            />
            <span>
              Mark rejected
              <span id={`${id}-reject`} className="block text-sm text-muted">
                Use when the bank shows no settled transfer. The payout becomes failed, and Retry
                becomes available.
                {wait > 0 ? ` Available in ${wait} seconds.` : ""}
              </span>
            </span>
          </label>
          {fields.action ? (
            <span id={`${id}-action-err`} className="text-danger">
              Choose what should happen.
            </span>
          ) : null}
        </fieldset>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-note`}>
            Note for the audit log (what you checked at the bank)
          </label>
          <textarea
            id={`${id}-note`}
            className="rounded border border-gray-400 p-2"
            name="note"
            rows={3}
            maxLength={NOTE_MAX}
            required
            value={note}
            onChange={(e) => setNote(e.target.value)}
            aria-invalid={fields.note ? true : undefined}
            aria-describedby={`${id}-count${fields.note ? ` ${id}-note-err` : ""}`}
          />
          <span id={`${id}-count`} aria-live="polite" className="text-sm text-muted">
            {note.length} of {NOTE_MAX} characters
          </span>
          {fields.note ? (
            <span id={`${id}-note-err`} className="text-danger">
              Enter a note of 1 to 500 characters.
            </span>
          ) : null}
        </div>
        {alertText ? (
          <div role="alert" className="text-danger">
            <p>{alertText}</p>
            {err instanceof ApiError && err.requestId ? (
              <p className="text-sm">Reference: {err.requestId}</p>
            ) : null}
          </div>
        ) : null}
        <div className="flex gap-2">
          <button
            type="submit"
            className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
            disabled={!canSubmit}
          >
            Resolve
          </button>
          <button type="button" className="rounded border px-4 py-2" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}
