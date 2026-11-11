import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { EmployeeDetail } from "@/components/employees/employee-detail";
import { Forbidden } from "@/components/forbidden";
import { requireSessionForPage } from "@/lib/require-session";
import { Uuid } from "@/lib/schemas/common";

export const metadata: Metadata = { title: "Employee" };

/** `/employees/[id]` (PAY-202, PAY-203): view any role; edit payroll_admin. */
export default async function EmployeePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!Uuid.safeParse(id).success) notFound();
  const s = await requireSessionForPage(`/employees/${id}`);
  if (s === "forbidden") return <Forbidden />;
  return (
    <EmployeeDetail
      employeeId={id}
      me={{ id: s.user_id, canEdit: s.roles.includes("payroll_admin") }}
    />
  );
}
