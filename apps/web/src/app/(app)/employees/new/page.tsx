import type { Metadata } from "next";

import { Forbidden } from "@/components/forbidden";
import { EmployeeForm } from "@/components/forms/employee-form";
import { requireSessionForPage } from "@/lib/require-session";

export const metadata: Metadata = { title: "Add employee" };

/** `/employees/new` (PAY-201): payroll_admin; others get <Forbidden/>. */
export default async function NewEmployeePage() {
  const s = await requireSessionForPage("/employees/new", ["payroll_admin"]);
  if (s === "forbidden") return <Forbidden />;
  return (
    <>
      <h1 className="text-2xl font-semibold">Add employee</h1>
      <EmployeeForm />
    </>
  );
}
