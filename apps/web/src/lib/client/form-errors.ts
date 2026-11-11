import type { z } from "zod";

import { ApiError } from "./api";

/**
 * Per-field form errors (web LLD §6.1: 422 errors show inline, per field, from `details`).
 * Shared, browser-side. `details[].field` is the C56 location (`body.bank_routing`), so the form
 * field is the segment after `body`; the issue code picks the message.
 */
export type FieldErrors = Record<string, string>;

const ISSUE_MESSAGES: Record<string, string> = {
  aba_checksum: "This routing number isn't valid. Check the 9 digits.",
  not_monday: "Choose a Monday.",
  timezone: "Choose a time zone from the list.",
  required_on_reactivation: "Enter new bank details to reactivate this employee.",
  bank_details_incomplete: "Enter both the routing and the account number.",
  duplicate_role: "Each role can be chosen once.",
  redrive_not_allowed: "Send again isn't possible: the bank key for this payment has expired.",
  too_small: "This is too short or too small.",
  too_big: "This is too long or too large.",
};

/** The message for one issue code (a generic one for anything unknown). */
export function issueMessage(issue: string): string {
  return ISSUE_MESSAGES[issue] ?? "Check this field.";
}

/** The form field a C56 location names: "body.entries.3.hours" → "entries"; "body" → "". */
function fieldOf(location: string): string {
  const parts = location.split(".");
  return parts[0] === "body" || parts[0] === "query" ? (parts[1] ?? "") : (parts[0] ?? "");
}

/** Field errors from an API 422 `validation_error`; `{}` for anything else. */
export function apiFieldErrors(err: unknown): FieldErrors {
  if (!(err instanceof ApiError) || err.code !== "validation_error") return {};
  if (!Array.isArray(err.details)) return {};
  const out: FieldErrors = {};
  for (const d of err.details as { field?: unknown; issue?: unknown }[]) {
    if (typeof d.field !== "string" || typeof d.issue !== "string") continue;
    const f = fieldOf(d.field);
    out[f] ??= issueMessage(d.issue);
  }
  return out;
}

/** Field errors from a client-side Zod parse (a custom refine's message is the issue code). */
export function zodFieldErrors(
  error: z.ZodError,
  messages: Record<string, string> = {},
): FieldErrors {
  const out: FieldErrors = {};
  for (const i of error.issues) {
    const f = String(i.path[0] ?? "");
    const code = i.code === "custom" ? i.message : i.code;
    out[f] ??= messages[f] ?? issueMessage(code);
  }
  return out;
}

/** The `employee_ids` an error forwarded (C15), for highlighting rows. */
export function errorEmployeeIds(err: unknown): string[] {
  if (!(err instanceof ApiError)) return [];
  const d = err.details as { employee_ids?: unknown } | undefined;
  return Array.isArray(d?.employee_ids)
    ? d.employee_ids.filter((x): x is string => typeof x === "string")
    : [];
}

/** Moves focus to the first invalid field after a failed submit (PAY-NFR-14). */
export function focusFirstInvalid(form: HTMLFormElement | null): void {
  form?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
}

/** The message for an error with no response at all (network down; §6.1). */
export const OFFLINE_MESSAGE = "The service is temporarily unavailable. Please try again.";

/** An error's display text: the mapped message, or the offline one. */
export function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : OFFLINE_MESSAGE;
}
