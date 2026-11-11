import { randomBytes } from "node:crypto";
import type { IdentitySchemas, PayrollSchemas } from "@payrun/api-client";
import type { z } from "zod";

import {
  AcceptBody,
  EmployeeCreateBody,
  EmployeeListQuery,
  EmployeePatchBody,
  HoursBody,
  InviteBody,
  isMonday,
  ResetConfirmBody,
  ResetRequestBody,
  ResolveBody,
  RunPayoutParam,
  RunPayslipParam,
  SettingsBody,
  timeZones,
  UserPatchBody,
} from "@/lib/schemas/requests";
import type { Employee, Page } from "@/lib/schemas/responses";
import {
  AuthAuditEvent,
  Hours,
  PayrollAuditEvent,
  PendingInvite,
  ResetConfirmResult,
  User,
} from "@/lib/schemas/responses";

import {
  authAuditEvent,
  EMPLOYEE_ID,
  hours,
  payrollAuditEvent,
  pendingInvite,
  user,
} from "../helpers/fixtures";

// Generated at runtime so no literal looks like a credential (testing.md §9).
const TOKEN = randomBytes(24).toString("base64url");
const ROUTING = "021000021";

describe("F5 response schemas mirror the service Out models (C5)", () => {
  // C5, PAY-602: the Zod types equal the generated api-client types, field for field.
  it("matches @payrun/api-client at the type level", () => {
    expectTypeOf<z.infer<typeof PendingInvite>>().toEqualTypeOf<
      IdentitySchemas["PendingInviteOut"]
    >();
    expectTypeOf<z.infer<typeof ResetConfirmResult>>().toEqualTypeOf<
      IdentitySchemas["ResetConfirmOut"]
    >();
    expectTypeOf<z.infer<typeof AuthAuditEvent>>().toEqualTypeOf<
      IdentitySchemas["AuditEntryOut"]
    >();
    expectTypeOf<z.infer<typeof Hours>>().toEqualTypeOf<PayrollSchemas["HoursOut"]>();
    expectTypeOf<z.infer<typeof PayrollAuditEvent>>().toEqualTypeOf<
      PayrollSchemas["AuditEventOut"]
    >();
    // C8: every list is Page<T>
    expectTypeOf<z.infer<ReturnType<typeof Page<typeof User>>>>().toEqualTypeOf<
      IdentitySchemas["UserListOut"]
    >();
    expectTypeOf<z.infer<ReturnType<typeof Page<typeof AuthAuditEvent>>>>().toEqualTypeOf<
      IdentitySchemas["AuditListOut"]
    >();
    expectTypeOf<z.infer<ReturnType<typeof Page<typeof Employee>>>>().toEqualTypeOf<
      PayrollSchemas["Page_EmployeeOut_"]
    >();
    expectTypeOf<z.infer<ReturnType<typeof Page<typeof PayrollAuditEvent>>>>().toEqualTypeOf<
      PayrollSchemas["Page_AuditEventOut_"]
    >();
  });

  // C5: request bodies are the service In models (equal, or assignable where the service type
  // shows optional fields as `| null`).
  it("request bodies fit the service In models", () => {
    expectTypeOf<z.infer<typeof InviteBody>>().toEqualTypeOf<IdentitySchemas["InviteUserIn"]>();
    expectTypeOf<z.infer<typeof AcceptBody>>().toEqualTypeOf<IdentitySchemas["InviteAcceptIn"]>();
    expectTypeOf<z.infer<typeof ResetRequestBody>>().toEqualTypeOf<
      IdentitySchemas["ResetRequestIn"]
    >();
    expectTypeOf<z.infer<typeof ResetConfirmBody>>().toEqualTypeOf<
      IdentitySchemas["ResetConfirmIn"]
    >();
    expectTypeOf<z.infer<typeof SettingsBody>>().toEqualTypeOf<
      PayrollSchemas["CompanySettingsIn"]
    >();
    expectTypeOf<z.infer<typeof HoursBody>>().toEqualTypeOf<PayrollSchemas["HoursIn"]>();
    expectTypeOf<z.infer<typeof ResolveBody>>().toEqualTypeOf<PayrollSchemas["ResolveIn"]>();
    expectTypeOf<z.infer<typeof UserPatchBody>>().toExtend<IdentitySchemas["PatchUserIn"]>();
    expectTypeOf<z.infer<typeof EmployeeCreateBody>>().toExtend<PayrollSchemas["EmployeeCreate"]>();
    expectTypeOf<z.infer<typeof EmployeePatchBody>>().toExtend<PayrollSchemas["EmployeeUpdate"]>();
  });

  // PAY-101, PAY-102, PAY-105, PAY-109, PAY-301: fixtures copied from the Out models parse.
  it("parses the fixtures", () => {
    expect(User.parse(user())).toEqual(user());
    expect(PendingInvite.parse(pendingInvite())).toEqual(pendingInvite());
    expect(
      PendingInvite.safeParse(
        pendingInvite({ expires_at: "2026-11-12T00:00:00Z", email_status: "sent" }),
      ).success,
    ).toBe(true);
    expect(PendingInvite.safeParse(pendingInvite({ email_status: "bounced" })).success).toBe(false);
    expect(Hours.parse(hours())).toEqual(hours());
    expect(Hours.safeParse(hours({ status: "calculated" })).success).toBe(false);
    expect(AuthAuditEvent.parse(authAuditEvent())).toEqual(authAuditEvent());
    expect(
      AuthAuditEvent.safeParse(
        authAuditEvent({ actor_user_id: null, target_user_id: null, ip_hash: null }),
      ).success,
    ).toBe(true);
    expect(PayrollAuditEvent.parse(payrollAuditEvent())).toEqual(payrollAuditEvent());
    expect(PayrollAuditEvent.safeParse(payrollAuditEvent({ entity_type: "user" })).success).toBe(
      false,
    );
    expect(ResetConfirmResult.safeParse({ user_id: EMPLOYEE_ID, roles_version: 3 }).success).toBe(
      true,
    );
  });
});

describe("F5 request schemas (§2.3)", () => {
  // PAY-103, PAY-105: the token is base64url 20–128; passwords ≥ 12.
  it("AcceptBody and ResetConfirmBody", () => {
    for (const S of [AcceptBody, ResetConfirmBody]) {
      expect(S.safeParse({ token: TOKEN, password: "x".repeat(12) }).success).toBe(true);
      expect(S.safeParse({ token: TOKEN, password: "x".repeat(11) }).success).toBe(false);
      expect(S.safeParse({ token: "a".repeat(19), password: "x".repeat(12) }).success).toBe(false);
      expect(S.safeParse({ token: "a".repeat(129), password: "x".repeat(12) }).success).toBe(false);
      expect(S.safeParse({ token: `${TOKEN}=`, password: "x".repeat(12) }).success).toBe(false);
      expect(S.safeParse({ token: TOKEN, password: "x".repeat(257) }).success).toBe(false);
    }
    expect(ResetRequestBody.parse({ email: " A@B.co " })).toEqual({ email: "a@b.co" });
  });

  // PAY-101: 1–3 unique roles.
  it("InviteBody", () => {
    const ok = { email: "a@b.co", name: " Pat ", roles: ["admin", "approver"] };
    expect(InviteBody.parse(ok).name).toBe("Pat");
    expect(InviteBody.safeParse({ ...ok, roles: [] }).success).toBe(false);
    expect(InviteBody.safeParse({ ...ok, roles: ["admin", "admin"] }).success).toBe(false);
    expect(InviteBody.safeParse({ ...ok, roles: ["owner"] }).success).toBe(false);
    expect(InviteBody.safeParse({ ...ok, name: "" }).success).toBe(false);
  });

  // PAY-108, R37, C25: status is "inactive" only; at least one field.
  it("UserPatchBody", () => {
    expect(UserPatchBody.safeParse({ status: "inactive" }).success).toBe(true);
    expect(UserPatchBody.safeParse({ roles: ["approver"] }).success).toBe(true);
    expect(UserPatchBody.safeParse({ status: "active" }).success).toBe(false);
    expect(UserPatchBody.safeParse({}).success).toBe(false);
    expect(UserPatchBody.safeParse({ roles: ["approver", "approver"] }).success).toBe(false);
  });

  // PAY-200: ranges, an IANA zone, a Monday anchor.
  it("SettingsBody", () => {
    const ok = {
      name: "Acme",
      tax_rate_bps: 5000,
      retirement_bps: 2000,
      timezone: "Europe/London",
      biweekly_anchor: "2026-11-09",
      require_dual_approval: false,
    };
    expect(SettingsBody.safeParse(ok).success).toBe(true);
    expect(SettingsBody.safeParse({ ...ok, tax_rate_bps: 5001 }).success).toBe(false);
    expect(SettingsBody.safeParse({ ...ok, retirement_bps: 2001 }).success).toBe(false);
    expect(SettingsBody.safeParse({ ...ok, timezone: "Nowhere/City" }).success).toBe(false);
    expect(SettingsBody.safeParse({ ...ok, biweekly_anchor: "2026-11-10" }).success).toBe(false);
    expect(SettingsBody.safeParse({ ...ok, require_dual_approval: "yes" }).success).toBe(false);
    expect(isMonday("2026-11-09")).toBe(true);
    expect(isMonday("2026-02-30")).toBe(false);
    expect(isMonday("nope")).toBe(false);
    expect(timeZones()).toContain("UTC");
    expect(timeZones()).toContain("America/New_York");
  });

  // PAY-201: a union on pay_type with the matching basis; the ABA checksum.
  it("EmployeeCreateBody", () => {
    const common = {
      name: "Ada",
      email: "ada@b.co",
      schedule: "monthly",
      bank_routing: ROUTING,
      bank_account: "1234",
    };
    expect(
      EmployeeCreateBody.safeParse({ ...common, pay_type: "salaried", annual_salary_cents: 1 })
        .success,
    ).toBe(true);
    expect(
      EmployeeCreateBody.safeParse({ ...common, pay_type: "hourly", hourly_rate_cents: 100_000 })
        .success,
    ).toBe(true);
    expect(
      EmployeeCreateBody.safeParse({ ...common, pay_type: "hourly", annual_salary_cents: 1 })
        .success,
    ).toBe(false);
    expect(
      EmployeeCreateBody.safeParse({
        ...common,
        pay_type: "salaried",
        annual_salary_cents: 1_000_000_001,
      }).success,
    ).toBe(false);
    expect(
      EmployeeCreateBody.safeParse({
        ...common,
        bank_routing: "021000022",
        pay_type: "salaried",
        annual_salary_cents: 1,
      }).success,
    ).toBe(false);
    expect(
      EmployeeCreateBody.safeParse({
        ...common,
        bank_account: "1".repeat(18),
        pay_type: "salaried",
        annual_salary_cents: 1,
      }).success,
    ).toBe(false);
  });

  // PAY-203: every field optional; at least one; status active/inactive.
  it("EmployeePatchBody and EmployeeListQuery", () => {
    expect(EmployeePatchBody.safeParse({ status: "active" }).success).toBe(true);
    expect(EmployeePatchBody.safeParse({ bank_account: "12345" }).success).toBe(true);
    expect(EmployeePatchBody.safeParse({}).success).toBe(false);
    expect(EmployeePatchBody.safeParse({ name: undefined }).success).toBe(false);
    expect(EmployeePatchBody.safeParse({ bank_routing: "123456789" }).success).toBe(false);
    expect(EmployeeListQuery.parse({ status: "active" })).toEqual({ status: "active", limit: 50 });
    expect(EmployeeListQuery.safeParse({ status: "pending" }).success).toBe(false);
  });

  // PAY-301: unique employees, 1–500 entries, 0–74,400 hundredths.
  it("HoursBody", () => {
    const e = (id: string, h: number) => ({ employee_id: id, hours_hundredths: h });
    expect(HoursBody.safeParse({ entries: [e(EMPLOYEE_ID, 0)] }).success).toBe(true);
    expect(HoursBody.safeParse({ entries: [e(EMPLOYEE_ID, 74_400)] }).success).toBe(true);
    expect(HoursBody.safeParse({ entries: [e(EMPLOYEE_ID, 74_401)] }).success).toBe(false);
    expect(HoursBody.safeParse({ entries: [e(EMPLOYEE_ID, -1)] }).success).toBe(false);
    expect(HoursBody.safeParse({ entries: [e(EMPLOYEE_ID, 1), e(EMPLOYEE_ID, 2)] }).success).toBe(
      false,
    );
    expect(HoursBody.safeParse({ entries: [] }).success).toBe(false);
  });

  // PAY-502: action enum; note trimmed, 1–500.
  it("ResolveBody and the run sub-resource params", () => {
    expect(ResolveBody.parse({ action: "redrive", note: "  ok  " })).toEqual({
      action: "redrive",
      note: "ok",
    });
    expect(ResolveBody.safeParse({ action: "mark_rejected", note: "x".repeat(500) }).success).toBe(
      true,
    );
    expect(ResolveBody.safeParse({ action: "mark_rejected", note: "x".repeat(501) }).success).toBe(
      false,
    );
    expect(ResolveBody.safeParse({ action: "retry", note: "x" }).success).toBe(false);
    expect(ResolveBody.safeParse({ action: "redrive", note: "" }).success).toBe(false);
    expect(RunPayoutParam.safeParse({ id: EMPLOYEE_ID, payoutId: EMPLOYEE_ID }).success).toBe(true);
    expect(RunPayslipParam.safeParse({ id: EMPLOYEE_ID, payslipId: "x" }).success).toBe(false);
  });
});
