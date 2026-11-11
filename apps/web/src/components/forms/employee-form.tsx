"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";

import {
  BankFields,
  bankErrors,
  EMPTY_BANK,
  type BankInput,
} from "@/components/employees/bank-fields";
import { FormError, SelectField, TextField } from "@/components/field";
import { ApiError, apiFetch } from "@/lib/client/api";
import {
  apiFieldErrors,
  errorText,
  focusFirstInvalid,
  zodFieldErrors,
  type FieldErrors,
} from "@/lib/client/form-errors";
import { queryKeys } from "@/lib/client/query-keys";
import { centsToInput, parseMoney } from "@/lib/money";
import { EmployeeCreateBody, EmployeePatchBody } from "@/lib/schemas/requests";
import type { Employee, Settings } from "@/lib/schemas/responses";

type PayType = "salaried" | "hourly";
type Schedule = "monthly" | "biweekly";

interface Values {
  name: string;
  email: string;
  pay_type: PayType;
  schedule: Schedule;
  salary: string;
  rate: string;
}

const MONEY_HINT = "Dollars, like 52000 or 52000.50.";

function initialValues(e?: Employee): Values {
  return {
    name: e?.name ?? "",
    email: e?.email ?? "",
    pay_type: e?.pay_type ?? "salaried",
    schedule: e?.schedule ?? "monthly",
    salary: centsToInput(e?.annual_salary_cents),
    rate: centsToInput(e?.hourly_rate_cents),
  };
}

/** Money text → cents for the chosen pay type, or a field error. */
function payBasis(v: Values): { field: string; cents: number | null; error?: string } {
  const field = v.pay_type === "salaried" ? "annual_salary_cents" : "hourly_rate_cents";
  const cents = parseMoney(v.pay_type === "salaried" ? v.salary : v.rate);
  return cents === null
    ? { field, cents, error: `Enter an amount. ${MONEY_HINT}` }
    : { field, cents };
}

/** Errors from a failed save: 409 `employee_email_taken` goes on the email field (§6.2). */
function saveErrors(err: unknown): FieldErrors {
  if (err instanceof ApiError && err.code === "employee_email_taken") return { email: err.message };
  return apiFieldErrors(err);
}

/**
 * Create (`/employees/new`, PAY-201) or edit (`/employees/[id]`, PAY-203) an employee. The pay
 * type switches between the salary and the rate field; money is entered as text and converted
 * with integer arithmetic (ADR 0006). Bank details are entered on create only: editing them is a
 * separate dialog that never shows the stored numbers.
 */
export function EmployeeForm({
  employee,
  onSaved,
}: {
  employee?: Employee;
  onSaved?: (e: Employee) => void;
}) {
  const editing = employee !== undefined;
  const router = useRouter();
  const qc = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const [v, setV] = useState<Values>(() => initialValues(employee));
  const [bank, setBank] = useState<BankInput>(EMPTY_BANK);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [saved, setSaved] = useState(false);
  const settings = useQuery({
    queryKey: queryKeys.settings,
    queryFn: () => apiFetch<Settings>("/api/settings"),
    enabled: !editing,
  });

  const save = useMutation({
    mutationFn: (body: unknown) =>
      editing
        ? apiFetch<Employee>(`/api/employees/${employee.id}`, { method: "PATCH", body })
        : apiFetch<Employee>("/api/employees", { method: "POST", body }),
    onSuccess: (e) => {
      qc.setQueryData(queryKeys.employee(e.id), e);
      void qc.invalidateQueries({ queryKey: ["employees"] });
      if (editing) {
        setSaved(true);
        onSaved?.(e);
      } else router.push(`/employees/${e.id}`);
    },
    onError: (err) => {
      setErrors(saveErrors(err));
      setTimeout(() => focusFirstInvalid(formRef.current), 0);
    },
  });

  function set<K extends keyof Values>(k: K, value: Values[K]) {
    setSaved(false);
    setV((old) => ({ ...old, [k]: value }));
  }

  function fail(fe: FieldErrors) {
    setErrors(fe);
    setTimeout(() => focusFirstInvalid(formRef.current), 0);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const basis = payBasis(v);
    if (basis.error) return fail({ [basis.field]: basis.error });
    if (!editing) {
      const be = bankErrors(bank);
      const parsed = EmployeeCreateBody.safeParse({
        name: v.name,
        email: v.email,
        schedule: v.schedule,
        pay_type: v.pay_type,
        [basis.field]: basis.cents,
        ...bank,
      });
      if (!parsed.success || Object.keys(be).length > 0) {
        return fail({ ...(parsed.success ? {} : zodFieldErrors(parsed.error)), ...be });
      }
      setErrors({});
      save.mutate(parsed.data);
      return;
    }
    // Edit: send only what changed (PAY-203). A pay-type switch sends the new basis too.
    const diff: Record<string, unknown> = {};
    if (v.name.trim() !== employee.name) diff.name = v.name;
    if (v.email.trim().toLowerCase() !== employee.email) diff.email = v.email;
    if (v.schedule !== employee.schedule) diff.schedule = v.schedule;
    if (v.pay_type !== employee.pay_type) diff.pay_type = v.pay_type;
    const old =
      v.pay_type === "salaried" ? employee.annual_salary_cents : employee.hourly_rate_cents;
    if (diff.pay_type !== undefined || basis.cents !== old) diff[basis.field] = basis.cents;
    if (Object.keys(diff).length === 0) {
      setErrors({});
      setSaved(true);
      return;
    }
    const parsed = EmployeePatchBody.safeParse(diff);
    if (!parsed.success) return fail(zodFieldErrors(parsed.error));
    setErrors({});
    save.mutate(parsed.data);
  }

  const err = save.error;
  // The email-taken message sits on the email field; anything else shows here too (§6.1).
  const fieldLevel = err instanceof ApiError && err.code === "employee_email_taken";
  return (
    <form ref={formRef} className="mt-4 flex max-w-md flex-col gap-4" onSubmit={submit} noValidate>
      <TextField
        label="Name"
        name="name"
        value={v.name}
        onChange={(x) => set("name", x)}
        error={errors.name}
      />
      <TextField
        label="Email"
        name="email"
        type="email"
        value={v.email}
        onChange={(x) => set("email", x)}
        error={errors.email}
      />
      <SelectField
        label="Pay type"
        name="pay_type"
        value={v.pay_type}
        onChange={(x) => set("pay_type", x)}
        options={[
          { value: "salaried", label: "Salaried" },
          { value: "hourly", label: "Hourly" },
        ]}
      />
      {v.pay_type === "salaried" ? (
        <TextField
          label="Annual salary (USD)"
          name="annual_salary_cents"
          inputMode="decimal"
          value={v.salary}
          onChange={(x) => set("salary", x)}
          error={errors.annual_salary_cents}
          hint={MONEY_HINT}
        />
      ) : (
        <TextField
          label="Hourly rate (USD)"
          name="hourly_rate_cents"
          inputMode="decimal"
          value={v.rate}
          onChange={(x) => set("rate", x)}
          error={errors.hourly_rate_cents}
          hint={MONEY_HINT}
        />
      )}
      <SelectField
        label="Pay schedule"
        name="schedule"
        value={v.schedule}
        onChange={(x) => set("schedule", x)}
        error={errors.schedule}
        options={[
          { value: "monthly", label: "Monthly" },
          { value: "biweekly", label: "Biweekly" },
        ]}
      />
      {!editing && v.schedule === "biweekly" && settings.data ? (
        <p className="text-sm text-muted">
          Biweekly periods start every 14 days from {settings.data.biweekly_anchor}.
        </p>
      ) : null}
      {!editing ? (
        <fieldset className="flex flex-col gap-4 rounded border p-3">
          <legend>Bank details</legend>
          <BankFields value={bank} onChange={setBank} errors={errors} />
        </fieldset>
      ) : null}
      <FormError
        message={err && !fieldLevel ? errorText(err) : null}
        requestId={err instanceof ApiError ? err.requestId : null}
      />
      {saved ? <p role="status">Saved.</p> : null}
      <button
        type="submit"
        className="w-fit rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
        disabled={save.isPending}
      >
        {editing ? "Save changes" : "Add employee"}
      </button>
    </form>
  );
}
