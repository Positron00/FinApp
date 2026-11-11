/** TanStack Query keys (web LLD §1.3). */
export const queryKeys = {
  me: ["me"] as const,
  settings: ["settings"] as const,
  payRuns: (cursor: string | null, status?: string) =>
    ["pay-runs", cursor, status ?? null] as const,
  payRun: (id: string) => ["pay-run", id] as const,
  payslips: (id: string, cursor: string | null) => ["pay-run", id, "payslips", cursor] as const,
  allPayslips: (id: string) => ["pay-run", id, "payslips", "all"] as const,
  payslip: (id: string, payslipId: string) => ["pay-run", id, "payslip", payslipId] as const,
  payouts: (id: string, cursor: string | null, status?: string) =>
    ["pay-run", id, "payouts", cursor, status ?? null] as const,
  employees: (cursor: string | null, status?: string) =>
    ["employees", cursor, status ?? null] as const,
  employee: (id: string) => ["employee", id] as const,
  users: (cursor: string | null) => ["users", cursor] as const,
  authAudit: (cursor: string | null) => ["audit", "auth", cursor] as const,
  payrollAudit: (cursor: string | null) => ["audit", "payroll", cursor] as const,
};

/** A list page from the BFF (C8). */
export interface PageOf<T> {
  items: T[];
  next_cursor: string | null;
}

/** `?limit=…&cursor=…[&status=…]` for a list call. */
export function listQuery(limit: number, cursor: string | null, status?: string): string {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (cursor) qs.set("cursor", cursor);
  if (status) qs.set("status", status);
  return qs.toString();
}
