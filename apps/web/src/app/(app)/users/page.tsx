import type { Metadata } from "next";

import { Forbidden } from "@/components/forbidden";
import { UsersPage } from "@/components/users/users-page";
import { requireSessionForPage } from "@/lib/require-session";

export const metadata: Metadata = { title: "Users" };

/** `/users` (PAY-101, PAY-102, PAY-108): admin; others get <Forbidden/>. */
export default async function UsersRoutePage() {
  const s = await requireSessionForPage("/users", ["admin"]);
  if (s === "forbidden") return <Forbidden />;
  return <UsersPage meId={s.user_id} />;
}
