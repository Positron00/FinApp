"use client";

import { ApiError } from "@/lib/client/api";

/**
 * An error with its reference (web LLD §6.1): 503 shows the "temporarily unavailable" banner
 * with a Retry button; others show the mapped message.
 */
export function ErrorBanner({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const api = error instanceof ApiError ? error : null;
  const message =
    api?.status === 503
      ? "The service is temporarily unavailable."
      : (api?.message ?? "Something went wrong. Please try again.");
  return (
    <div role="alert" className="my-3 rounded border border-danger p-3 text-danger">
      <p>{message}</p>
      {api?.requestId ? <p className="text-sm">Reference: {api.requestId}</p> : null}
      {onRetry ? (
        <button type="button" className="mt-2 underline" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}
