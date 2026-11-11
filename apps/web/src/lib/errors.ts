import "server-only";

import { z } from "zod";

import { logger } from "./logger";

/**
 * The BFF error catalogue and upstream error mapping (web LLD §3.3, PAY-602, C14, C15, C34).
 * Every error response is `{error: {code, message, request_id, details?}}`. Messages come from
 * this catalogue, never from a service.
 */

export type Target = "identity" | "payroll";

const ValidationDetails = z
  .array(z.object({ field: z.string().max(200), issue: z.string().max(100) }))
  .max(100);
const EmployeeIdsDetails = z.object({ employee_ids: z.array(z.uuid()).max(500) });

export type ErrorDetails = z.infer<typeof ValidationDetails> | z.infer<typeof EmployeeIdsDetails>;

/** Values a message template may use. */
export interface MessageVars {
  requestId?: string;
  retryAfter?: number;
  count?: number;
  schedule?: "monthly" | "biweekly";
  route?: string;
}

type Msg = string | ((v: MessageVars) => string);

const n = (v: MessageVars) => String(v.count ?? 0);

/** code → [status, message]. A status of 0 means "as upstream". */
export const CATALOG: Record<string, readonly [number, Msg]> = {
  unauthenticated: [401, "Your session has ended. Please sign in again."],
  forbidden_role: [403, "You don't have permission to do that."],
  bad_origin: [403, "This request was blocked for your security. Reload the page and try again."],
  payload_too_large: [413, "That request is too large."],
  invalid_json: [400, "The request couldn't be read."],
  bad_request: [400, "The request couldn't be processed."],
  not_found: [404, "That page or item wasn't found."],
  method_not_allowed: [405, "That action isn't supported here."],
  unsupported_media_type: [415, "That request format isn't supported."],
  conflict: [409, "That change conflicts with the current state. Reload and try again."],
  idempotency_key_required: [
    400,
    "This action was sent without its safety key. Reload the page and try again.",
  ],
  validation_error: [422, "Some fields need attention."],
  rate_limited: [429, (v) => `Too many requests. Try again in ${v.retryAfter ?? 60} seconds.`],
  service_unavailable: [503, "The service is temporarily unavailable. Please try again."],
  internal_error: [500, (v) => `Something went wrong. Reference: ${v.requestId ?? "unknown"}.`],
  invalid_credentials: [401, "Email or password is incorrect."],
  invite_invalid: [
    400,
    "This invite link has expired or was already used. Ask your admin to send a new one.",
  ],
  password_too_weak: [422, "Use a password of at least 12 characters."],
  reset_invalid: [400, "This reset link has expired or was already used. Request a new one."],
  user_email_taken: [409, "A user with this email already exists."],
  invite_not_pending: [409, "This user has already accepted their invite."],
  cannot_modify_self: [409, "You can't change your own roles or status. Ask another admin."],
  last_admin: [409, "The company must keep at least one active admin."],
  user_not_found: [404, "That user wasn't found."],
  invite_not_found: [404, "That invite wasn't found."],
  company_settings_not_found: [404, "Company settings haven't been set up yet. Ask an admin."],
  anchor_locked: [409, "The biweekly anchor can't change after biweekly runs exist."],
  employee_email_taken: [409, "An employee with this email already exists."],
  employee_not_found: [404, "That employee wasn't found."],
  pay_run_exists: [409, "A pay run already exists for this schedule and period."],
  invalid_period_start: [
    422,
    (v) =>
      v.schedule === "biweekly"
        ? "Biweekly runs must start on a period start date (every 14 days from the company's biweekly anchor)."
        : "Monthly runs must start on the 1st of a month.",
  ],
  no_employees: [409, "No active employees are on this schedule."],
  employee_in_overlapping_run: [
    409,
    (v) => `${n(v)} employee(s) are already in another run that covers these dates.`,
  ],
  pay_run_locked: [409, "This run is locked because it has been approved."],
  not_hourly: [422, "Hours can only be entered for hourly employees."],
  employee_not_in_run: [
    422,
    (v) => `${n(v)} employee(s) aren't in this run. Reload the run and try again.`,
  ],
  hours_missing: [
    422,
    (v) => `Enter hours for every hourly employee before calculating (${n(v)} missing).`,
  ],
  pay_run_not_approvable: [
    409,
    "This run can't be approved: it has already been approved, or it isn't calculated.",
  ],
  idempotency_key_reused: [
    422,
    "This approval was already submitted with different details. Reload and try again.",
  ],
  approver_must_differ: [
    403,
    (v) =>
      v.route?.endsWith("/retry")
        ? "Dual approval is on: someone else must retry. You marked this payment rejected, or changed this employee's bank details and a second person hasn't released them yet."
        : "Dual approval is on: someone else must approve. You created, calculated or entered hours for this run, or changed bank details that a second person hasn't released yet.",
  ],
  payout_not_retryable: [
    409,
    "Only failed payouts can be retried. Payouts awaiting the bank or blocked can't be retried.",
  ],
  retry_limit_reached: [409, "This payout has reached the limit of 5 attempts."],
  employee_bank_details_missing: [
    409,
    (v) =>
      v.route?.endsWith("/retry")
        ? "This employee is inactive or has no bank details. Update the employee first."
        : "Some employees in this run have no bank details. Add their bank details, then approve.",
  ],
  payout_not_blocked: [409, "This payout isn't blocked any more. Refresh the page."],
  resolve_too_early: [
    409,
    (v) =>
      `This payout was blocked moments ago. Wait ${v.retryAfter ?? 0} seconds, check the bank again, then mark it rejected.`,
  ],
  pay_run_not_found: [404, "That pay run wasn't found."],
  payslip_not_found: [404, "That payslip wasn't found."],
  payout_not_found: [404, "That payout wasn't found."],
  request_failed: [0, "The request couldn't be completed."],
};

/**
 * Codes whose `details.employee_ids` is forwarded (C15). `approver_must_differ` isn't one: its
 * `details.rule` (calculator, creator, hours_editor, bank_editor, resolver) is payroll-internal
 * and is dropped here; the one message per route covers every rule (C73).
 */
const EMPLOYEE_ID_CODES = new Set([
  "not_hourly",
  "employee_not_in_run",
  "employee_in_overlapping_run",
  "hours_missing",
]);

/** Upstream codes that mean "temporarily unavailable" (PAY-605). */
const UNAVAILABLE_CODES = new Set(["db_timeout", "db_unavailable", "service_unavailable"]);

export interface BffErrorInit {
  details?: ErrorDetails;
  retryAfter?: number;
  clearCookie?: boolean;
  vars?: MessageVars;
}

export class BffError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: ErrorDetails;
  readonly retryAfter?: number;
  readonly clearCookie: boolean;
  readonly vars: MessageVars;

  constructor(status: number, code: string, init: BffErrorInit = {}) {
    super(code);
    this.name = "BffError";
    this.status = status;
    this.code = code;
    this.details = init.details;
    this.retryAfter = init.retryAfter;
    this.clearCookie = init.clearCookie ?? false;
    this.vars = init.vars ?? {};
  }
}

/** A BffError for a catalogue code, at the catalogue's status. */
export function bffError(code: string, init: BffErrorInit = {}): BffError {
  const entry = CATALOG[code];
  return new BffError(
    entry && entry[0] !== 0 ? entry[0] : 500,
    entry ? code : "internal_error",
    init,
  );
}

export function messageFor(code: string, vars: MessageVars): string {
  const entry = CATALOG[code] ?? CATALOG.request_failed!;
  const m = entry[1];
  return typeof m === "function" ? m(vars) : m;
}

function parseRetryAfter(h: string | null): number | undefined {
  if (h === null) return undefined;
  const s = Number(h);
  return Number.isFinite(s) && s >= 0 ? Math.ceil(s) : undefined;
}

export interface UpstreamFailure {
  status: number;
  body: unknown;
  headers: Headers;
}

/**
 * Maps a failed service response to a BffError (web LLD §3.3). Pure apart from logging.
 * `vars` supplies context the message needs (the run schedule for `invalid_period_start`).
 */
export function mapUpstreamError(
  target: Target,
  res: UpstreamFailure,
  routeName: string,
  vars: MessageVars = {},
): BffError {
  const parsed = z
    .object({ error: z.object({ code: z.string().max(100), details: z.unknown().optional() }) })
    .safeParse(res.body);
  const code = parsed.success ? parsed.data.error.code : undefined;
  const rawDetails = parsed.success ? parsed.data.error.details : undefined;
  const retryAfter = parseRetryAfter(res.headers.get("retry-after"));
  const allVars: MessageVars = { ...vars, route: routeName, retryAfter };

  if (res.status === 401 && code === "invalid_token") {
    logger().error(
      { event: "bff.upstream_auth_failed", target, operation: routeName },
      "upstream rejected our token",
    );
    return new BffError(500, "internal_error", { vars: allVars });
  }
  if ([502, 503, 504].includes(res.status) || (code && UNAVAILABLE_CODES.has(code))) {
    return new BffError(503, "service_unavailable", { vars: allVars });
  }
  if (code && code in CATALOG && code !== "request_failed") {
    let details: ErrorDetails | undefined;
    if (code === "validation_error") {
      const d = ValidationDetails.safeParse(rawDetails);
      details = d.success ? d.data : undefined;
    } else if (EMPLOYEE_ID_CODES.has(code)) {
      const d = EmployeeIdsDetails.safeParse(rawDetails);
      details = d.success ? { employee_ids: d.data.employee_ids } : undefined;
      allVars.count = d.success ? d.data.employee_ids.length : 0;
    }
    return new BffError(res.status, code, { details, retryAfter, vars: allVars });
  }
  if (res.status >= 400 && res.status < 500) {
    logger().warn(
      { event: "bff.unmapped_error", target, status: res.status, code: code ?? null },
      "unmapped upstream error",
    );
    return new BffError(res.status, "request_failed", { retryAfter, vars: allVars });
  }
  return new BffError(500, "internal_error", { vars: allVars });
}

/** The PAY-602 body for an error. */
export function errorBody(err: BffError, requestId: string) {
  return {
    error: {
      code: err.code,
      message: messageFor(err.code, { ...err.vars, requestId }),
      request_id: requestId,
      ...(err.details !== undefined ? { details: err.details } : {}),
    },
  };
}

/** Any thrown value as a BffError: unknown errors become 500 `internal_error`. */
export function asBffError(err: unknown): BffError {
  return err instanceof BffError ? err : new BffError(500, "internal_error");
}

/** Zod issues as `[{field, issue}]` with the C56 location prefix, never the input value (C14). */
export function zodDetails(prefix: string, error: z.ZodError): ErrorDetails {
  return error.issues.slice(0, 100).map((i) => ({
    field: [prefix, ...i.path.map(String)]
      .filter((p) => p !== "")
      .join(".")
      .slice(0, 200),
    issue: i.code.slice(0, 100),
  }));
}
