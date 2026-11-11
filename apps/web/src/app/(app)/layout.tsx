import { headers } from "next/headers";
import type { ReactNode } from "react";

import { AppShell } from "@/components/app-shell";
import { Forbidden } from "@/components/forbidden";
import { requireSessionForPage } from "@/lib/require-session";

/**
 * The signed-in shell (web LLD §1.1). The layout's check isn't enough on its own (layouts aren't
 * re-rendered on client navigation), so every page under (app) calls requireSessionForPage too.
 */
export default async function AppLayout({ children }: { children: ReactNode }) {
  // proxy.ts passes the path, so a sign-in redirect can come back here.
  const path = (await headers()).get("x-pathname") ?? "/dashboard";
  const s = await requireSessionForPage(path);
  if (s === "forbidden") return <Forbidden />;
  return <AppShell user={{ id: s.user_id, name: s.name, roles: s.roles }}>{children}</AppShell>;
}
