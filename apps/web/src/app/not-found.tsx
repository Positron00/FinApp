import Link from "next/link";

export default function NotFound() {
  return (
    <main id="main" className="mx-auto max-w-xl p-8">
      <h1 className="text-2xl font-semibold">Page not found</h1>
      <p className="mt-2 text-muted">That page or item wasn&apos;t found.</p>
      <Link className="mt-4 inline-block text-brand underline" href="/dashboard">
        Go to the dashboard
      </Link>
    </main>
  );
}
