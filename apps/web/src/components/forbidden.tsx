import Link from "next/link";

/** Rendered when the user can't open a page at all (web LLD §6.1). */
export function Forbidden() {
  return (
    <section className="mx-auto max-w-xl p-8">
      <h1 className="text-2xl font-semibold">You don&apos;t have access to this page</h1>
      <p className="mt-2 text-muted">You don&apos;t have permission to do that.</p>
      <Link className="mt-4 inline-block text-brand underline" href="/dashboard">
        Go to the dashboard
      </Link>
    </section>
  );
}
