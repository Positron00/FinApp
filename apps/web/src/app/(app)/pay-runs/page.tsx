import type { Metadata } from "next";

import { Forbidden } from "@/components/forbidden";
import { RunList } from "@/components/runs/run-list";
import { requireSessionForPage } from "@/lib/require-session";

export const metadata: Metadata = { title: "Pay runs" };

/** `/pay-runs` (PAY-303): any role. */
export default async function PayRunsPage() {
  const s = await requireSessionForPage("/pay-runs");
  if (s === "forbidden") return <Forbidden />;
  return <RunList canCreate={s.roles.includes("payroll_admin")} />;
}
