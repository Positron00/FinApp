/** TanStack Query keys (web LLD §1.3). */
export const queryKeys = {
  me: ["me"] as const,
  payRuns: (cursor: string | null) => ["pay-runs", cursor] as const,
  payRun: (id: string) => ["pay-run", id] as const,
  payslips: (id: string, cursor: string | null) => ["pay-run", id, "payslips", cursor] as const,
  payouts: (id: string, cursor: string | null) => ["pay-run", id, "payouts", cursor] as const,
};
