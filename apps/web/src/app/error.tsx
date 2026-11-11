"use client";

/** The error boundary: a mapped message and the digest as a reference, never a stack (§1.1). */
export default function ErrorPage({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main id="main" className="mx-auto max-w-xl p-8">
      <h1 className="text-2xl font-semibold">Something went wrong</h1>
      <p className="mt-2 text-muted" role="alert">
        The service is temporarily unavailable, or something went wrong. Please try again.
        {error.digest ? ` Reference: ${error.digest}.` : ""}
      </p>
      <button
        type="button"
        className="mt-4 rounded bg-brand px-4 py-2 text-white"
        onClick={() => reset()}
      >
        Try again
      </button>
    </main>
  );
}
