import type { Metadata } from "next";
import Link from "next/link";

import { ResetFlow } from "@/components/forms/reset-forms";

export const metadata: Metadata = { title: "Reset your password" };

/** `/reset` (PAY-105): public. Request a link, or confirm one (`#token`, never sent to the server). */
export default function ResetPage() {
  return (
    <main id="main" className="mx-auto mt-16 max-w-sm p-6">
      <h1 className="text-2xl font-semibold">Reset your password</h1>
      <ResetFlow />
      <Link className="mt-6 inline-block text-brand underline" href="/login">
        Back to sign in
      </Link>
    </main>
  );
}
