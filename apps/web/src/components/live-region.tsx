"use client";

/** A polite live region (PAY-NFR-14). The caller changes `message` only when something changed. */
export function LiveRegion({ message }: { message: string }) {
  return (
    <div aria-live="polite" role="status" className="sr-only">
      {message}
    </div>
  );
}
