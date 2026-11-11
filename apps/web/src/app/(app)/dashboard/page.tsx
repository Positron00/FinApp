import type { Metadata } from "next";
import Link from "next/link";

import { Forbidden } from "@/components/forbidden";
import { requireSessionForPage } from "@/lib/require-session";

export const metadata: Metadata = { title: "Dashboard" };

/** `/dashboard`: the landing page after sign-in. The cards arrive with F5. */
export default async function DashboardPage() {
  const s = await requireSessionForPage("/dashboard");
  if (s === "forbidden") return <Forbidden />;
  return (
    <>
      <h1 className="text-2xl font-semibold">Dashboard</h1>
      <p className="mt-2 text-muted">Welcome. Pay runs are where payroll happens.</p>
      <Link className="mt-4 inline-block text-brand underline" href="/pay-runs">
        Go to pay runs
      </Link>
    </>
  );
}
