import type { Metadata } from "next";

import { AuditPage } from "@/components/audit/audit-page";
import { Forbidden } from "@/components/forbidden";
import { requireSessionForPage } from "@/lib/require-session";

export const metadata: Metadata = { title: "Audit log" };

/** `/audit` (PAY-109, plan §8): admin; others get <Forbidden/>. */
export default async function AuditRoutePage() {
  const s = await requireSessionForPage("/audit", ["admin"]);
  if (s === "forbidden") return <Forbidden />;
  return <AuditPage />;
}
