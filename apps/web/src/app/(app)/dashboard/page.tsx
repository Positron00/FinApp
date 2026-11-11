import type { Metadata } from "next";

import { Dashboard } from "@/components/dashboard";
import { Forbidden } from "@/components/forbidden";
import { requireSessionForPage } from "@/lib/require-session";

export const metadata: Metadata = { title: "Dashboard" };

/** `/dashboard` (PAY-303, PAY-413): the landing page after sign-in; any role. */
export default async function DashboardPage() {
  const s = await requireSessionForPage("/dashboard");
  if (s === "forbidden") return <Forbidden />;
  return <Dashboard canPrepare={s.roles.includes("payroll_admin")} />;
}
