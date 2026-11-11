import type { Metadata } from "next";
import Link from "next/link";

import { LoginForm } from "@/components/forms/login-form";

export const metadata: Metadata = { title: "Sign in" };

const RESET_DONE = "Password changed. Sign in with your new password.";

/** `/login` (PAY-104): public. `?reset=1` shows the PAY-105 "password changed" notice. */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const next = sp.next;
  return (
    <main id="main" className="mx-auto mt-16 max-w-sm p-6">
      <h1 className="text-2xl font-semibold">Sign in to PayRun</h1>
      {sp.reset === "1" ? (
        <p role="status" className="mt-4 rounded border border-brand p-3">
          {RESET_DONE}
        </p>
      ) : null}
      <LoginForm next={typeof next === "string" ? next : undefined} />
      <Link className="mt-4 inline-block text-brand underline" href="/reset">
        Forgot password?
      </Link>
    </main>
  );
}
