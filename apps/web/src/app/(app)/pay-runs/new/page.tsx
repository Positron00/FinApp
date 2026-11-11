import type { Metadata } from "next";

import { Forbidden } from "@/components/forbidden";
import { RunCreateForm } from "@/components/forms/run-create-form";
import { requireSessionForPage } from "@/lib/require-session";

export const metadata: Metadata = { title: "New pay run" };

/** `/pay-runs/new` (PAY-300): payroll_admin; others get <Forbidden/>. */
export default async function NewPayRunPage() {
  const s = await requireSessionForPage("/pay-runs/new", ["payroll_admin"]);
  if (s === "forbidden") return <Forbidden />;
  return (
    <>
      <h1 className="text-2xl font-semibold">New pay run</h1>
      <RunCreateForm />
    </>
  );
}
