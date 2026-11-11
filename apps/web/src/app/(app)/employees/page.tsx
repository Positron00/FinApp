import type { Metadata } from "next";

import { EmployeeList } from "@/components/employees/employee-list";
import { Forbidden } from "@/components/forbidden";
import { requireSessionForPage } from "@/lib/require-session";

export const metadata: Metadata = { title: "Employees" };

/** `/employees` (PAY-202): any role; "Add employee" for payroll_admin. */
export default async function EmployeesPage() {
  const s = await requireSessionForPage("/employees");
  if (s === "forbidden") return <Forbidden />;
  return <EmployeeList canCreate={s.roles.includes("payroll_admin")} />;
}
