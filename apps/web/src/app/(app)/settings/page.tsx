import type { Metadata } from "next";

import { Forbidden } from "@/components/forbidden";
import { SettingsView } from "@/components/forms/settings-form";
import { requireSessionForPage } from "@/lib/require-session";

export const metadata: Metadata = { title: "Company settings" };

/** `/settings` (PAY-200): view any role; edit admin. */
export default async function SettingsPage() {
  const s = await requireSessionForPage("/settings");
  if (s === "forbidden") return <Forbidden />;
  return (
    <>
      <h1 className="text-2xl font-semibold">Company settings</h1>
      <SettingsView me={{ id: s.user_id, isAdmin: s.roles.includes("admin") }} />
    </>
  );
}
