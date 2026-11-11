import type { Metadata } from "next";

import { AcceptForm } from "@/components/forms/accept-form";

export const metadata: Metadata = { title: "Accept your invite" };

/** `/accept` (PAY-103): public. The token is in the fragment, which never reaches the server. */
export default function AcceptPage() {
  return (
    <main id="main" className="mx-auto mt-16 max-w-sm p-6">
      <h1 className="text-2xl font-semibold">Accept your invite</h1>
      <p className="mt-2 text-muted">Choose a password to finish setting up your account.</p>
      <AcceptForm />
    </main>
  );
}
