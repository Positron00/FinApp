/**
 * Response fixtures copied from the service Out models (C5): every field of PayRunOut,
 * PayslipOut, PayoutOut/AttemptOut, EmployeeOut, SessionUser and MeOut.
 */
import { COMPANY_ID } from "./env";
import { USER_ID } from "./request";

export const RUN_ID = "0193a000-0000-7000-8000-0000000000b1";
export const PAYSLIP_ID = "0193a000-0000-7000-8000-0000000000c1";
export const PAYOUT_ID = "0193a000-0000-7000-8000-0000000000d1";
export const EMPLOYEE_ID = "0193a000-0000-7000-8000-0000000000e1";
export const TS = "2026-11-10T12:00:00.123456Z";

export const sessionUser = {
  user_id: USER_ID,
  company_id: COMPANY_ID,
  email: "owner@payrun.example.com",
  name: "Olive Owner",
  roles: ["admin", "payroll_admin", "approver"],
  roles_version: 1,
  status: "active",
};

export const meOut = {
  id: USER_ID,
  email: "owner@payrun.example.com",
  name: "Olive Owner",
  roles: ["admin", "payroll_admin", "approver"],
  roles_version: 1,
  status: "active",
  created_at: TS,
  updated_at: TS,
  activated_at: TS,
  deactivated_at: null,
  pending_invite_id: null,
  company_id: COMPANY_ID,
};

export const counts = {
  requested: 0,
  awaiting_bank: 0,
  blocked: 0,
  paid: 0,
  no_transfer: 0,
  failed: 0,
};

export function payRun(over: Record<string, unknown> = {}) {
  return {
    id: RUN_ID,
    schedule: "monthly",
    period_start: "2026-11-01",
    period_end: "2026-11-30",
    pay_date: "2026-11-30",
    period_index: 11,
    periods_per_year: 12,
    status: "calculated",
    employee_count: 5,
    tax_bps: 2000,
    retirement_bps: 500,
    require_dual_approval: true,
    totals: {
      gross_cents: 2_500_000,
      tax_cents: 500_000,
      retirement_cents: 125_000,
      net_cents: 1_875_000,
      funding_required_cents: 1_875_000,
    },
    counts: { ...counts },
    created_by: USER_ID,
    created_at: TS,
    calculated_by: USER_ID,
    calculated_by_name: "Olive Owner",
    calculated_at: TS,
    approved_by: null,
    approved_by_name: null,
    approved_at: null,
    processing_since: null,
    updated_at: TS,
    ...over,
  };
}

export function payslip(over: Record<string, unknown> = {}) {
  return {
    id: PAYSLIP_ID,
    pay_run_id: RUN_ID,
    employee_id: EMPLOYEE_ID,
    employee_name: "Ada Employee",
    pay_type: "salaried",
    annual_salary_cents: 6_000_000,
    hourly_rate_cents: null,
    hours_hundredths: null,
    status: "calculated",
    gross_cents: 500_000,
    tax_cents: 100_000,
    retirement_cents: 25_000,
    net_cents: 375_000,
    ...over,
  };
}

export function attempt(over: Record<string, unknown> = {}) {
  return {
    attempt: 1,
    status: "settled",
    failure_reason: null,
    bank_reference: "BANK-REF-1",
    account_last4: "6789",
    requested_at: TS,
    final_at: TS,
    ...over,
  };
}

export function payout(over: Record<string, unknown> = {}) {
  return {
    id: PAYOUT_ID,
    pay_run_id: RUN_ID,
    payslip_id: PAYSLIP_ID,
    employee_id: EMPLOYEE_ID,
    employee_name: "Ada Employee",
    amount_cents: 375_000,
    currency: "USD",
    status: "paid",
    no_transfer: false,
    current_attempt: 1,
    attempts: [attempt()],
    ...over,
  };
}

export function employee(over: Record<string, unknown> = {}) {
  return {
    id: EMPLOYEE_ID,
    name: "Ada Employee",
    email: "ada@payrun.example.com",
    pay_type: "salaried",
    annual_salary_cents: 6_000_000,
    hourly_rate_cents: null,
    schedule: "monthly",
    account_last4: null,
    has_bank_details: false,
    bank_updated_at: null,
    bank_updated_by: USER_ID,
    status: "active",
    deactivated_at: null,
    created_at: TS,
    updated_at: TS,
    ...over,
  };
}

export function errorBody(code: string, details?: unknown) {
  return {
    error: {
      code,
      message: "upstream internal message: SELECT * FROM secrets",
      request_id: "upstream-req",
      ...(details !== undefined ? { details } : {}),
    },
  };
}

// ---- F5 fixtures (identity UserOut, PendingInviteOut, AuditEntryOut; payroll
// CompanySettingsOut, HoursOut, AuditEventOut), every field as the service models (C5).

export const TARGET_USER_ID = "0193a000-0000-7000-8000-0000000000a3";
export const INVITE_ID = "0193a000-0000-7000-8000-0000000000f1";
export const AUDIT_ID = "0193a000-0000-7000-8000-0000000000f2";

export function user(over: Record<string, unknown> = {}) {
  return {
    id: TARGET_USER_ID,
    email: "pat@payrun.example.com",
    name: "Pat Pending",
    roles: ["payroll_admin"],
    roles_version: 1,
    status: "pending",
    created_at: TS,
    updated_at: TS,
    activated_at: null,
    deactivated_at: null,
    pending_invite_id: INVITE_ID,
    ...over,
  };
}

export function pendingInvite(over: Record<string, unknown> = {}) {
  return { id: INVITE_ID, expires_at: null, email_status: "pending", ...over };
}

export function settings(over: Record<string, unknown> = {}) {
  return {
    name: "Acme Ltd",
    tax_rate_bps: 2000,
    retirement_bps: 500,
    timezone: "America/New_York",
    biweekly_anchor: "2026-01-05",
    require_dual_approval: true,
    updated_at: TS,
    updated_by: USER_ID,
    ...over,
  };
}

export function hours(over: Record<string, unknown> = {}) {
  return {
    pay_run_id: RUN_ID,
    status: "draft",
    entries: [{ employee_id: EMPLOYEE_ID, hours_hundredths: 8050 }],
    ...over,
  };
}

export function authAuditEvent(over: Record<string, unknown> = {}) {
  return {
    id: AUDIT_ID,
    at: TS,
    actor_user_id: USER_ID,
    event: "user.invited",
    target_user_id: TARGET_USER_ID,
    ip_hash: "abcd1234abcd1234",
    details: { roles: ["payroll_admin"] },
    ...over,
  };
}

export function payrollAuditEvent(over: Record<string, unknown> = {}) {
  return {
    id: AUDIT_ID,
    at: TS,
    actor_id: USER_ID,
    event: "employee.updated",
    entity_type: "employee",
    entity_id: EMPLOYEE_ID,
    details: { fields: ["name"] },
    ...over,
  };
}
