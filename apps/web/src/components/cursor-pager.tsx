"use client";

import { useState } from "react";

/**
 * Cursor paging (web LLD §6.1, C8): a stack of the cursors seen; Next sends `next_cursor`,
 * Previous pops. The cursor is opaque and stays in component state, not in the URL.
 */
export function useCursorStack() {
  const [stack, setStack] = useState<(string | null)[]>([null]);
  const cursor = stack[stack.length - 1] ?? null;
  return {
    cursor,
    hasPrevious: stack.length > 1,
    next: (c: string) => setStack((s) => [...s, c]),
    previous: () => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s)),
    reset: () => setStack([null]),
  };
}

export function CursorPager({
  nextCursor,
  hasPrevious,
  onNext,
  onPrevious,
}: {
  nextCursor: string | null;
  hasPrevious: boolean;
  onNext: (c: string) => void;
  onPrevious: () => void;
}) {
  return (
    <nav aria-label="Pagination" className="mt-3 flex gap-2">
      <button
        type="button"
        className="rounded border px-3 py-1 disabled:opacity-50"
        disabled={!hasPrevious}
        onClick={onPrevious}
      >
        Previous
      </button>
      <button
        type="button"
        className="rounded border px-3 py-1 disabled:opacity-50"
        disabled={nextCursor === null}
        onClick={() => nextCursor !== null && onNext(nextCursor)}
      >
        Next
      </button>
    </nav>
  );
}
