"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Dialog } from "@/components/dialog";
import { ErrorBanner } from "@/components/error-banner";
import { FormError } from "@/components/field";
import { EmployeeForm } from "@/components/forms/employee-form";
import { ApiError, apiFetch } from "@/lib/client/api";
import { apiFieldErrors, errorText, type FieldErrors } from "@/lib/client/form-errors";
import { queryKeys } from "@/lib/client/query-keys";
import type { Employee } from "@/lib/schemas/responses";

import { BankFields, bankErrors, EMPTY_BANK, type BankInput } from "./bank-fields";
import { bankLabel, payBasisLabel } from "./employee-list";

/** A user reference: "you", else the first 8 characters of the ID (§6.1). */
function whoShort(id: string | null, meId: string): string {
  if (id === null) return "—";
  return id === meId ? "you" : id.slice(0, 8);
}

type DialogKind = "bank" | "deactivate" | "reactivate" | null;

/**
 * `/employees/[id]` (web LLD §6.2; PAY-202, PAY-203, ADR 0004): view for every role; edit,
 * "Change bank details" and deactivate/reactivate for payroll_admin. Bank details are only ever
 * shown as `••••last4`. Reactivating an employee whose bank details were purged (PAY-608) asks
 * for new ones and sends them in the same PATCH.
 */
export function EmployeeDetail({
  employeeId,
  me,
}: {
  employeeId: string;
  me: { id: string; canEdit: boolean };
}) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: queryKeys.employee(employeeId),
    queryFn: () => apiFetch<Employee>(`/api/employees/${employeeId}`),
  });
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (q.isPending) return <p>Loading…</p>;
  if (!q.data) return <ErrorBanner error={q.error} onRetry={() => void q.refetch()} />;
  const e = q.data;

  function done(updated: Employee, message: string) {
    qc.setQueryData(queryKeys.employee(employeeId), updated);
    void qc.invalidateQueries({ queryKey: ["employees"] });
    setDialog(null);
    setNotice(message);
  }

  return (
    <>
      <h1 className="text-2xl font-semibold">{e.name}</h1>
      {notice ? (
        <p role="status" className="mt-2">
          {notice}
        </p>
      ) : null}
      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-1">
        <dt className="text-muted">Email</dt>
        <dd>{e.email}</dd>
        <dt className="text-muted">Pay</dt>
        <dd>{payBasisLabel(e)}</dd>
        <dt className="text-muted">Schedule</dt>
        <dd>{e.schedule === "monthly" ? "Monthly" : "Biweekly"}</dd>
        <dt className="text-muted">Bank</dt>
        <dd>{bankLabel(e)}</dd>
        <dt className="text-muted">Bank details changed</dt>
        <dd>
          {e.bank_updated_at ? new Date(e.bank_updated_at).toLocaleString() : "—"} by{" "}
          {whoShort(e.bank_updated_by, me.id)}
        </dd>
        <dt className="text-muted">Status</dt>
        <dd>
          {e.status === "active" ? "Active" : "Inactive"}
          {e.deactivated_at ? ` (since ${new Date(e.deactivated_at).toLocaleDateString()})` : ""}
        </dd>
      </dl>

      {me.canEdit ? (
        <>
          <div className="mt-4 flex gap-2">
            <button
              type="button"
              className="rounded border px-4 py-2"
              onClick={() => setDialog("bank")}
            >
              Change bank details
            </button>
            {e.status === "active" ? (
              <button
                type="button"
                className="rounded border border-danger px-4 py-2 text-danger"
                onClick={() => setDialog("deactivate")}
              >
                Deactivate
              </button>
            ) : (
              <button
                type="button"
                className="rounded border px-4 py-2"
                onClick={() => setDialog("reactivate")}
              >
                Reactivate
              </button>
            )}
          </div>
          <h2 className="mt-6 text-xl font-semibold">Edit details</h2>
          <EmployeeForm employee={e} />
        </>
      ) : null}

      {dialog === "bank" ? (
        <BankDialog
          employee={e}
          title="Change bank details"
          extra={{}}
          submitLabel="Save bank details"
          onClose={() => setDialog(null)}
          onDone={(u) => done(u, "Bank details changed.")}
        />
      ) : null}
      {dialog === "reactivate" && !e.has_bank_details ? (
        <BankDialog
          employee={e}
          title="Reactivate employee"
          intro="This employee's bank details were removed while inactive. Enter new bank details to reactivate."
          extra={{ status: "active" }}
          submitLabel="Reactivate"
          onClose={() => setDialog(null)}
          onDone={(u) => done(u, "Employee reactivated.")}
        />
      ) : null}
      {dialog === "deactivate" || (dialog === "reactivate" && e.has_bank_details) ? (
        <StatusDialog
          employee={e}
          to={dialog === "deactivate" ? "inactive" : "active"}
          onClose={() => setDialog(null)}
          onDone={(u) =>
            done(u, dialog === "deactivate" ? "Employee deactivated." : "Employee reactivated.")
          }
        />
      ) : null}
    </>
  );
}

function usePatch(employeeId: string, onDone: (e: Employee) => void) {
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiFetch<Employee>(`/api/employees/${employeeId}`, { method: "PATCH", body }),
    retry: 0,
    onSuccess: onDone,
  });
}

/** New routing and account numbers (never prefilled), optionally with a status change. */
function BankDialog({
  employee,
  title,
  intro,
  extra,
  submitLabel,
  onClose,
  onDone,
}: {
  employee: Employee;
  title: string;
  intro?: string;
  extra: Record<string, unknown>;
  submitLabel: string;
  onClose: () => void;
  onDone: (e: Employee) => void;
}) {
  const [bank, setBank] = useState<BankInput>(EMPTY_BANK);
  const [errors, setErrors] = useState<FieldErrors>({});
  const patch = usePatch(employee.id, onDone);
  const err = patch.error;
  const serverFields = apiFieldErrors(err);

  return (
    <Dialog title={title} onClose={onClose}>
      <form
        className="mt-3 flex flex-col gap-4"
        noValidate
        onSubmit={(ev) => {
          ev.preventDefault();
          const be = bankErrors(bank);
          setErrors(be);
          if (Object.keys(be).length > 0) {
            ev.currentTarget.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
            return;
          }
          patch.mutate({ ...extra, ...bank });
        }}
      >
        {intro ? <p>{intro}</p> : null}
        <p className="text-sm text-muted">
          Current: {bankLabel(employee)}. The full numbers are never shown.
        </p>
        <BankFields value={bank} onChange={setBank} errors={{ ...serverFields, ...errors }} />
        <FormError
          message={err ? errorText(err) : null}
          requestId={err instanceof ApiError ? err.requestId : null}
        />
        <div className="flex gap-2">
          <button
            type="submit"
            className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
            disabled={patch.isPending}
          >
            {submitLabel}
          </button>
          <button type="button" className="rounded border px-4 py-2" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

/** Deactivate, or reactivate when bank details are still on file: a confirm (`alertdialog`). */
function StatusDialog({
  employee,
  to,
  onClose,
  onDone,
}: {
  employee: Employee;
  to: "active" | "inactive";
  onClose: () => void;
  onDone: (e: Employee) => void;
}) {
  const patch = usePatch(employee.id, onDone);
  const err = patch.error;
  return (
    <Dialog
      title={to === "inactive" ? "Deactivate employee" : "Reactivate employee"}
      onClose={onClose}
      alert
    >
      <p className="mt-3">
        {to === "inactive"
          ? `${employee.name} won't be included in new pay runs.`
          : `${employee.name} will be included in new pay runs again.`}
      </p>
      <FormError
        message={err ? errorText(err) : null}
        requestId={err instanceof ApiError ? err.requestId : null}
      />
      <div className="mt-4 flex gap-2">
        <button
          type="button"
          className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
          disabled={patch.isPending}
          onClick={() => patch.mutate({ status: to })}
        >
          {to === "inactive" ? "Deactivate" : "Reactivate"}
        </button>
        <button type="button" className="rounded border px-4 py-2" onClick={onClose}>
          Cancel
        </button>
      </div>
    </Dialog>
  );
}
