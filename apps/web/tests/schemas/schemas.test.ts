import type { IdentitySchemas, PayrollSchemas } from "@payrun/api-client";
import type { z } from "zod";

import {
  IdempotencyKeyHeader,
  IdParam,
  ListQuery,
  LoginBody,
  PayoutListQuery,
  PayRunCreateBody,
  PayRunListQuery,
  TelemetryBody,
} from "@/lib/schemas/requests";
import type { Page, RunCounts, RunTotals } from "@/lib/schemas/responses";
import {
  Attempt,
  Employee,
  Me,
  PayRun,
  Payout,
  Payslip,
  SessionUser,
  Settings,
  toMe,
  User,
  UserMe,
} from "@/lib/schemas/responses";

import {
  attempt,
  employee,
  meOut,
  payout,
  payRun,
  payslip,
  sessionUser,
  TS,
} from "../helpers/fixtures";

describe("response schemas mirror the service Out models (C5)", () => {
  // C5, PAY-602: the Zod types equal the generated api-client types, field for field.
  it("matches @payrun/api-client at the type level", () => {
    expectTypeOf<z.infer<typeof SessionUser>>().toEqualTypeOf<IdentitySchemas["SessionUser"]>();
    expectTypeOf<z.infer<typeof User>>().toEqualTypeOf<IdentitySchemas["UserOut"]>();
    expectTypeOf<z.infer<typeof UserMe>>().toEqualTypeOf<IdentitySchemas["MeOut"]>();
    expectTypeOf<z.infer<typeof Settings>>().toEqualTypeOf<PayrollSchemas["CompanySettingsOut"]>();
    expectTypeOf<z.infer<typeof Employee>>().toEqualTypeOf<PayrollSchemas["EmployeeOut"]>();
    expectTypeOf<z.infer<typeof RunTotals>>().toEqualTypeOf<PayrollSchemas["RunTotalsOut"]>();
    expectTypeOf<z.infer<typeof RunCounts>>().toEqualTypeOf<PayrollSchemas["PayoutCounts"]>();
    expectTypeOf<z.infer<typeof PayRun>>().toEqualTypeOf<PayrollSchemas["PayRunOut"]>();
    expectTypeOf<z.infer<typeof Payslip>>().toEqualTypeOf<PayrollSchemas["PayslipOut"]>();
    expectTypeOf<z.infer<typeof Attempt>>().toEqualTypeOf<PayrollSchemas["AttemptOut"]>();
    expectTypeOf<z.infer<typeof Payout>>().toEqualTypeOf<PayrollSchemas["PayoutOut"]>();
    expectTypeOf<z.infer<ReturnType<typeof Page<typeof PayRun>>>>().toEqualTypeOf<
      PayrollSchemas["Page_PayRunOut_"]
    >();
    // C8: list query parameters match the service's
    expectTypeOf<z.infer<typeof PayRunCreateBody>>().toEqualTypeOf<
      PayrollSchemas["PayRunCreate"]
    >();
  });

  // PAY-303, R21, C9: PayRun with all six counts and the *_name fields.
  it("parses a PayRun fixture and rejects a missing count", () => {
    expect(PayRun.parse(payRun())).toEqual(payRun());
    const draft = payRun({
      status: "draft",
      totals: null,
      tax_bps: null,
      retirement_bps: null,
      require_dual_approval: null,
    });
    expect(PayRun.safeParse(draft).success).toBe(true);
    const { blocked: _b, ...fiveCounts } = payRun().counts;
    expect(PayRun.safeParse(payRun({ counts: fiveCounts })).success).toBe(false);
    expect(PayRun.safeParse(payRun({ status: "approved" })).success).toBe(false);
  });

  // PAY-303, C5: a pending payslip has null amounts and no schedule.
  it("parses payslips", () => {
    expect(
      Payslip.safeParse(
        payslip({
          status: "pending",
          gross_cents: null,
          tax_cents: null,
          retirement_cents: null,
          net_cents: null,
        }),
      ).success,
    ).toBe(true);
    expect(Payslip.parse({ ...payslip(), schedule: "monthly" })).not.toHaveProperty("schedule"); // stripped
  });

  // PAY-501, C5, PAY-NFR-08: Payout and Attempt are strict.
  it("Payout and Attempt are strict", () => {
    expect(Payout.safeParse(payout()).success).toBe(true);
    expect(
      Payout.safeParse(payout({ no_transfer: true, current_attempt: 0, attempts: [] })).success,
    ).toBe(true);
    expect(Payout.safeParse({ ...payout(), bank_account: "123456789" }).success).toBe(false);
    expect(Attempt.safeParse({ ...attempt(), routing: "021000021" }).success).toBe(false);
    expect(Attempt.safeParse(attempt({ account_last4: "12345" })).success).toBe(false);
    expect(Attempt.safeParse(attempt({ bank_reference: "x".repeat(65) })).success).toBe(false);
    expect(Payout.safeParse(payout({ currency: "EUR" })).success).toBe(false);
  });

  // PAY-202, C5: Employee is strict; account_last4 may be null after the purge.
  it("Employee is strict and allows a null account_last4", () => {
    expect(Employee.safeParse(employee()).success).toBe(true);
    expect(
      Employee.safeParse(employee({ account_last4: "6789", has_bank_details: true })).success,
    ).toBe(true);
    expect(Employee.safeParse({ ...employee(), bank_account: "000123456789" }).success).toBe(false);
    expect(Employee.safeParse(employee({ account_last4: "12" })).success).toBe(false);
  });

  // PAY-108, C11: User with pending_invite_id.
  it("parses users and settings", () => {
    const { company_id: _c, ...user } = meOut;
    expect(
      User.safeParse({
        ...user,
        status: "pending",
        pending_invite_id: "0193a000-0000-7000-8000-0000000000f1",
      }).success,
    ).toBe(true);
    expect(UserMe.safeParse(meOut).success).toBe(true);
    expect(
      Settings.safeParse({
        name: "Acme",
        tax_rate_bps: 2000,
        retirement_bps: 500,
        timezone: "America/New_York",
        biweekly_anchor: "2026-01-05",
        require_dual_approval: true,
        updated_at: TS,
        updated_by: null,
      }).success,
    ).toBe(true);
  });

  // PAY-104, C6: SessionUser → Me with id = user_id and no company_id; UserMe → Me.
  it("maps SessionUser and UserMe to Me", () => {
    const fromSession = toMe(SessionUser.parse(sessionUser));
    expect(Me.parse(fromSession)).toEqual({
      id: sessionUser.user_id,
      email: sessionUser.email,
      name: sessionUser.name,
      roles: sessionUser.roles,
      roles_version: 1,
      status: "active",
    });
    expect(fromSession).not.toHaveProperty("company_id");
    expect(toMe(UserMe.parse(meOut))).toEqual(fromSession);
    expect(SessionUser.safeParse({ ...sessionUser, status: "inactive" }).success).toBe(false);
  });
});

describe("request schemas (§2.3)", () => {
  // PAY-104: the email is trimmed and lower-cased; no minimum password length at sign-in.
  it("LoginBody", () => {
    expect(LoginBody.parse({ email: " A@B.Example.com ", password: "x" })).toEqual({
      email: "a@b.example.com",
      password: "x",
    });
    expect(LoginBody.safeParse({ email: "nope", password: "x" }).success).toBe(false);
    expect(LoginBody.safeParse({ email: "a@b.co", password: "" }).success).toBe(false);
    expect(LoginBody.safeParse({ email: "a@b.co", password: "x".repeat(257) }).success).toBe(false);
    expect(LoginBody.safeParse({ email: "a@b.co", password: "x", extra: 1 }).success).toBe(false); // strict
  });

  // C8: cursor and limit.
  it("ListQuery and its filters", () => {
    expect(ListQuery.parse({})).toEqual({ limit: 50 });
    expect(ListQuery.parse({ limit: "100", cursor: "abc_-1" })).toEqual({
      limit: 100,
      cursor: "abc_-1",
    });
    expect(ListQuery.safeParse({ limit: "0" }).success).toBe(false);
    expect(ListQuery.safeParse({ cursor: "a/b" }).success).toBe(false);
    expect(ListQuery.safeParse({ cursor: "a".repeat(513) }).success).toBe(false);
    expect(PayRunListQuery.safeParse({ status: "partially_failed" }).success).toBe(true);
    expect(PayRunListQuery.safeParse({ status: "approved" }).success).toBe(false);
    expect(PayoutListQuery.safeParse({ status: "failed" }).success).toBe(true);
  });

  // PAY-300, PAY-400, C34.
  it("PayRunCreateBody, IdParam and the idempotency key", () => {
    expect(
      PayRunCreateBody.safeParse({ schedule: "monthly", period_start: "2026-11-01" }).success,
    ).toBe(true);
    expect(
      PayRunCreateBody.safeParse({ schedule: "weekly", period_start: "2026-11-01" }).success,
    ).toBe(false);
    expect(
      PayRunCreateBody.safeParse({ schedule: "monthly", period_start: "11/01/2026" }).success,
    ).toBe(false);
    expect(IdParam.safeParse({ id: "0193a000-0000-7000-8000-0000000000b1" }).success).toBe(true);
    expect(IdParam.safeParse({ id: "x" }).success).toBe(false);
    expect(IdempotencyKeyHeader.safeParse(crypto.randomUUID()).success).toBe(true);
    expect(IdempotencyKeyHeader.safeParse("abc").success).toBe(false);
  });

  // §7.5: telemetry body.
  it("TelemetryBody", () => {
    expect(
      TelemetryBody.safeParse({ type: "vital", name: "LCP", value: 1, rating: "good", route: "/" })
        .success,
    ).toBe(true);
    expect(
      TelemetryBody.safeParse({ type: "vital", name: "FID", value: 1, rating: "good", route: "/" })
        .success,
    ).toBe(false);
    expect(
      TelemetryBody.safeParse({ type: "error", message: "x".repeat(501), route: "/" }).success,
    ).toBe(false);
    // PAY-NFR-07, C63: `route` is a template, never free text.
    for (const route of ["/pay-runs/[id]", "/login", "/"]) {
      expect(TelemetryBody.safeParse({ type: "error", message: "m", route }).success).toBe(true);
    }
    for (const route of ["free text", "/a b", "/x?token=1", "/X", "pay-runs"]) {
      expect(TelemetryBody.safeParse({ type: "error", message: "m", route }).success).toBe(false);
    }
  });
});
