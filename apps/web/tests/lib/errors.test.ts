import {
  BffError,
  CATALOG,
  errorBody,
  mapUpstreamError,
  messageFor,
  zodDetails,
} from "@/lib/errors";
import { z } from "zod";

import { errorBody as upstream } from "../helpers/fixtures";
import { t } from "../helpers/state";

/** PAY-602, C14, C15, C34, C56: the catalogue and mapUpstreamError, one case per §3.3 row. */
const h = (o: Record<string, string> = {}) => new Headers(o);
const map = (status: number, code: string, route = "/api/x", details?: unknown, headers = h()) =>
  mapUpstreamError("payroll", { status, body: upstream(code, details), headers }, route);

const ROWS: [code: string, status: number, message: string][] = [
  ["forbidden_role", 403, "You don't have permission to do that."],
  ["payload_too_large", 413, "That request is too large."],
  ["bad_request", 400, "The request couldn't be processed."],
  ["not_found", 404, "That page or item wasn't found."],
  ["method_not_allowed", 405, "That action isn't supported here."],
  [
    "idempotency_key_required",
    400,
    "This action was sent without its safety key. Reload the page and try again.",
  ],
  ["validation_error", 422, "Some fields need attention."],
  ["invalid_credentials", 401, "Email or password is incorrect."],
  [
    "invite_invalid",
    400,
    "This invite link has expired or was already used. Ask your admin to send a new one.",
  ],
  ["password_too_weak", 422, "Use a password of at least 12 characters."],
  ["reset_invalid", 400, "This reset link has expired or was already used. Request a new one."],
  ["user_email_taken", 409, "A user with this email already exists."],
  ["invite_not_pending", 409, "This user has already accepted their invite."],
  ["cannot_modify_self", 409, "You can't change your own roles or status. Ask another admin."],
  ["last_admin", 409, "The company must keep at least one active admin."],
  ["user_not_found", 404, "That user wasn't found."],
  ["invite_not_found", 404, "That invite wasn't found."],
  ["company_settings_not_found", 404, "Company settings haven't been set up yet. Ask an admin."],
  ["anchor_locked", 409, "The biweekly anchor can't change after biweekly runs exist."],
  ["employee_email_taken", 409, "An employee with this email already exists."],
  ["employee_not_found", 404, "That employee wasn't found."],
  ["pay_run_exists", 409, "A pay run already exists for this schedule and period."],
  ["invalid_period_start", 422, "Monthly runs must start on the 1st of a month."],
  ["no_employees", 409, "No active employees are on this schedule."],
  ["pay_run_locked", 409, "This run is locked because it has been approved."],
  ["not_hourly", 422, "Hours can only be entered for hourly employees."],
  [
    "pay_run_not_approvable",
    409,
    "This run can't be approved: it has already been approved, or it isn't calculated.",
  ],
  [
    "idempotency_key_reused",
    422,
    "This approval was already submitted with different details. Reload and try again.",
  ],
  [
    "payout_not_retryable",
    409,
    "Only failed payouts can be retried. Payouts awaiting the bank or blocked can't be retried.",
  ],
  ["retry_limit_reached", 409, "This payout has reached the limit of 5 attempts."],
  ["payout_not_blocked", 409, "This payout isn't blocked any more. Refresh the page."],
  ["pay_run_not_found", 404, "That pay run wasn't found."],
  ["payslip_not_found", 404, "That payslip wasn't found."],
  ["payout_not_found", 404, "That payout wasn't found."],
];

describe("mapUpstreamError", () => {
  // PAY-602: known codes keep status and code; the message is the BFF's, never upstream's.
  it.each(ROWS)("maps %s", (code, status, message) => {
    const e = map(status, code);
    expect(e).toMatchObject({ status, code });
    const body = errorBody(e, "req-1");
    expect(body.error.message).toBe(message);
    expect(JSON.stringify(body)).not.toContain("SELECT");
  });

  // PAY-401, PAY-500: route-specific approver_must_differ and employee_bank_details_missing.
  it("uses route-specific messages", () => {
    expect(messageFor("approver_must_differ", { route: "/api/pay-runs/[id]/approve" })).toMatch(
      /approve this run/,
    );
    expect(
      messageFor("approver_must_differ", { route: "/api/pay-runs/[id]/payouts/[payoutId]/retry" }),
    ).toMatch(/retry this payout/);
    expect(
      messageFor("employee_bank_details_missing", { route: "/api/pay-runs/[id]/approve" }),
    ).toMatch(/then approve/);
    expect(messageFor("employee_bank_details_missing", { route: "/x/retry" })).toBe(
      "This employee is inactive or has no bank details. Update the employee first.",
    );
    expect(messageFor("invalid_period_start", { schedule: "biweekly" })).toMatch(/^Biweekly runs/);
  });

  // PAY-502, C48: resolve_too_early and rate_limited carry Retry-After into the message.
  it("forwards Retry-After", () => {
    const e = map(409, "resolve_too_early", "/r", undefined, h({ "retry-after": "42" }));
    expect(e.retryAfter).toBe(42);
    expect(errorBody(e, "r").error.message).toMatch(/Wait 42 seconds/);
    const rl = map(429, "rate_limited", "/r", undefined, h({ "retry-after": "7" }));
    expect(errorBody(rl, "r").error.message).toBe("Too many requests. Try again in 7 seconds.");
  });

  // PAY-602: unknown 4xx → request_failed with the upstream status, logged.
  it("maps an unknown 4xx to request_failed", () => {
    const e = map(418, "teapot");
    expect(e).toMatchObject({ status: 418, code: "request_failed" });
    expect(errorBody(e, "r").error.message).toBe("The request couldn't be completed.");
    expect(t.logs.events("bff.unmapped_error")[0]).toMatchObject({ status: 418, code: "teapot" });
  });

  // PAY-602: unknown 5xx → 500 internal_error.
  it("maps an unknown 5xx to internal_error", () => {
    const e = map(500, "boom");
    expect(e).toMatchObject({ status: 500, code: "internal_error" });
    expect(errorBody(e, "abc").error.message).toBe("Something went wrong. Reference: abc.");
  });

  // PAY-605: 503s and db_timeout / db_unavailable → service_unavailable; 502/504 too.
  it.each([
    [503, "db_timeout"],
    [503, "db_unavailable"],
    [502, "whatever"],
    [504, "whatever"],
  ])("maps %i %s to service_unavailable", (status, code) => {
    expect(map(status, code)).toMatchObject({ status: 503, code: "service_unavailable" });
  });

  // PAY-600: an upstream 401 invalid_token is our fault → 500 + bff.upstream_auth_failed.
  it("maps invalid_token to 500 and logs it", () => {
    expect(map(401, "invalid_token")).toMatchObject({ status: 500, code: "internal_error" });
    expect(t.logs.events("bff.upstream_auth_failed")).toHaveLength(1);
  });

  // C14: validation details are re-parsed to [{field, issue}], extra keys stripped.
  it("re-parses validation details and strips extra keys", () => {
    const e = map(422, "validation_error", "/x", [
      { field: "body.email", issue: "value_error", input: "secret@x.com" },
    ]);
    expect(e.details).toEqual([{ field: "body.email", issue: "value_error" }]);
    expect(JSON.stringify(errorBody(e, "r"))).not.toContain("secret@x.com");
    expect(map(422, "validation_error", "/x", "not a list").details).toBeUndefined();
  });

  // C15: employee_ids are forwarded only for the four codes.
  it("forwards employee_ids only for the listed codes", () => {
    const ids = ["0193a000-0000-7000-8000-0000000000e1"];
    for (const code of [
      "not_hourly",
      "employee_not_in_run",
      "employee_in_overlapping_run",
      "hours_missing",
    ]) {
      expect(map(422, code, "/x", { employee_ids: ids, extra: 1 }).details).toEqual({
        employee_ids: ids,
      });
    }
    expect(map(409, "pay_run_exists", "/x", { employee_ids: ids }).details).toBeUndefined();
    expect(messageFor("employee_not_in_run", { count: 3 })).toBe(
      "3 employee(s) aren't in this run. Reload the run and try again.",
    );
  });

  // PAY-602: a body that isn't an error envelope.
  it("handles a non-JSON error body", () => {
    expect(
      mapUpstreamError("identity", { status: 404, body: undefined, headers: h() }, "/x"),
    ).toMatchObject({
      status: 404,
      code: "request_failed",
    });
  });

  // PAY-602: every catalogue entry has a message.
  it("has a message for every code", () => {
    for (const code of Object.keys(CATALOG)) expect(messageFor(code, {}).length).toBeGreaterThan(0);
    expect(new BffError(401, "unauthenticated")).toBeInstanceOf(Error);
  });
});

describe("zodDetails", () => {
  // C14, C56: location-prefixed dotted paths, issue codes, no values.
  it("builds [{field, issue}] without values", () => {
    const r = z
      .object({ entries: z.array(z.object({ h: z.number() })) })
      .safeParse({ entries: [{ h: "secret" }] });
    expect(zodDetails("body", r.error!)).toEqual([
      { field: "body.entries.0.h", issue: "invalid_type" },
    ]);
  });
});
