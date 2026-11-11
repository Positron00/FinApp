import type { Metadata } from "next";

import { LoginForm } from "@/components/forms/login-form";

export const metadata: Metadata = { title: "Sign in" };

/** `/login` (PAY-104): public. */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const next = (await searchParams).next;
  return (
    <main id="main" className="mx-auto mt-16 max-w-sm p-6">
      <h1 className="text-2xl font-semibold">Sign in to PayRun</h1>
      <LoginForm next={typeof next === "string" ? next : undefined} />
    </main>
  );
}
