"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { CursorPager, useCursorStack } from "@/components/cursor-pager";
import { ErrorBanner } from "@/components/error-banner";
import { SelectField } from "@/components/field";
import { apiFetch } from "@/lib/client/api";
import { listQuery, queryKeys, type PageOf } from "@/lib/client/query-keys";
import { formatCents } from "@/lib/money";
import type { Employee } from "@/lib/schemas/responses";

type StatusFilter = "" | "active" | "inactive";

/** "••••6789", or "No bank details" after a purge (PAY-202, PAY-608); never the full number. */
export function bankLabel(e: Pick<Employee, "account_last4" | "has_bank_details">): string {
  return e.has_bank_details && e.account_last4 ? `••••${e.account_last4}` : "No bank details";
}

/** "$52,000.00 / year" or "$25.00 / hour". */
export function payBasisLabel(
  e: Pick<Employee, "pay_type" | "annual_salary_cents" | "hourly_rate_cents">,
): string {
  return e.pay_type === "salaried"
    ? `${formatCents(e.annual_salary_cents)} / year`
    : `${formatCents(e.hourly_rate_cents)} / hour`;
}

/**
 * The employee list (web LLD §6.2 `/employees`, PAY-202): status filter, cursor pager (C8); the
 * bank column shows the last 4 digits only. "Add employee" for payroll_admin.
 */
export function EmployeeList({ canCreate }: { canCreate: boolean }) {
  const pager = useCursorStack();
  const [status, setStatus] = useState<StatusFilter>("");
  const q = useQuery({
    queryKey: queryKeys.employees(pager.cursor, status),
    queryFn: () =>
      apiFetch<PageOf<Employee>>(
        `/api/employees?${listQuery(50, pager.cursor, status || undefined)}`,
      ),
  });

  return (
    <>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Employees</h1>
        {canCreate ? (
          <Link className="rounded bg-brand px-4 py-2 text-white" href="/employees/new">
            Add employee
          </Link>
        ) : null}
      </div>
      <div className="mt-4 max-w-xs">
        <SelectField
          label="Status"
          name="status"
          value={status}
          onChange={(s) => {
            setStatus(s);
            pager.reset(); // a new filter starts from the first page (C8)
          }}
          options={[
            { value: "", label: "All" },
            { value: "active", label: "Active" },
            { value: "inactive", label: "Inactive" },
          ]}
        />
      </div>
      {q.isError ? <ErrorBanner error={q.error} onRetry={() => void q.refetch()} /> : null}
      {q.isPending ? <p className="mt-4">Loading…</p> : null}
      {q.data ? (
        <>
          <table className="mt-4 w-full text-left">
            <caption className="sr-only">Employees</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Email</th>
                <th scope="col">Pay type</th>
                <th scope="col">Pay basis</th>
                <th scope="col">Schedule</th>
                <th scope="col">Bank</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {q.data.items.map((e) => (
                <tr key={e.id} className="border-t">
                  <td>
                    <Link className="text-brand underline" href={`/employees/${e.id}`}>
                      {e.name}
                    </Link>
                  </td>
                  <td>{e.email}</td>
                  <td>{e.pay_type === "salaried" ? "Salaried" : "Hourly"}</td>
                  <td>{payBasisLabel(e)}</td>
                  <td>{e.schedule === "monthly" ? "Monthly" : "Biweekly"}</td>
                  <td>{bankLabel(e)}</td>
                  <td>{e.status === "active" ? "Active" : "Inactive"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {q.data.items.length === 0 ? <p className="mt-3">No employees.</p> : null}
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
