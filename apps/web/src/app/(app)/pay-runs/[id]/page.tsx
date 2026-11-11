import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { Forbidden } from "@/components/forbidden";
import { RunDetail } from "@/components/runs/run-detail";
import { requireSessionForPage } from "@/lib/require-session";
import { Uuid } from "@/lib/schemas/common";

export const metadata: Metadata = { title: "Pay run" };

/** `/pay-runs/[id]` (PAY-303, PAY-400, PAY-413): view any role; actions by role. */
export default async function PayRunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!Uuid.safeParse(id).success) notFound();
  const s = await requireSessionForPage(`/pay-runs/${id}`);
  if (s === "forbidden") return <Forbidden />;
  return <RunDetail runId={id} me={{ id: s.user_id, roles: s.roles }} />;
}
