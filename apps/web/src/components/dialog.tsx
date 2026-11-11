"use client";

import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * A modal dialog (web LLD §6.3 accessibility notes, PAY-NFR-14): `role="dialog"` or
 * `"alertdialog"` with `aria-modal`, labelled by its title; focus starts on `initialFocus` (else
 * the first focusable element), is trapped with Tab/Shift+Tab, Escape closes, and focus returns
 * to the element that opened it. Rendered only while open.
 */
export function Dialog({
  title,
  onClose,
  children,
  alert = false,
  initialFocus,
  describedBy,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** `role="alertdialog"` for destructive confirms and the resolve dialog. */
  alert?: boolean;
  initialFocus?: RefObject<HTMLElement | null>;
  describedBy?: string;
}) {
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const first =
      initialFocus?.current ?? ref.current?.querySelector<HTMLElement>(FOCUSABLE) ?? ref.current;
    first?.focus();
    return () => opener?.focus?.();
    // Mount and unmount only: focus moves in once and returns once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Escape") {
      e.stopPropagation();
      onCloseRef.current();
      return;
    }
    if (e.key !== "Tab" || !ref.current) return;
    const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (items.length === 0) return;
    const firstEl = items[0]!;
    const lastEl = items[items.length - 1]!;
    if (e.shiftKey && document.activeElement === firstEl) {
      e.preventDefault();
      lastEl.focus();
    } else if (!e.shiftKey && document.activeElement === lastEl) {
      e.preventDefault();
      firstEl.focus();
    }
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/40 p-4">
      <div
        ref={ref}
        role={alert ? "alertdialog" : "dialog"}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={describedBy}
        tabIndex={-1}
        className="max-h-full w-full max-w-lg overflow-auto rounded bg-white p-5 shadow"
        onKeyDown={onKeyDown}
      >
        <h2 id={titleId} className="text-lg font-semibold">
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}

/** A destructive confirm (`alertdialog`) with Confirm and Cancel; Cancel gets the first focus. */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  pending,
  onConfirm,
  onClose,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  pending?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  return (
    <Dialog title={title} onClose={onClose} alert initialFocus={cancelRef}>
      <div className="mt-3">{children}</div>
      <div className="mt-4 flex gap-2">
        <button
          type="button"
          className="rounded bg-danger px-4 py-2 text-white disabled:opacity-50"
          disabled={pending}
          onClick={onConfirm}
        >
          {confirmLabel}
        </button>
        <button
          ref={cancelRef}
          type="button"
          className="rounded border px-4 py-2"
          onClick={onClose}
        >
          Cancel
        </button>
      </div>
    </Dialog>
  );
}
