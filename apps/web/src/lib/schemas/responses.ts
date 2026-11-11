import { z } from "zod";

import { Cents, DateTime, IsoDate, PayType, RoleEnum, Schedule, Uuid } from "./common";

/**
 * Response schemas (web LLD §2.4). They mirror the service Out models field for field (C5):
 * same names, nullability and enums; the `expectTypeOf` checks in `tests/schemas/schemas.test.ts` fail when they drift from
 * `@payrun/api-client`. Default mode strips unknown keys; `Employee`, `Payout` and `Attempt` are
 * strict, so an unexpected key (a full bank number, say) fails parsing (PAY-202, PAY-NFR-08).
 */

export const Page = <T extends z.ZodType>(item: T) =>
  z.object({ items: z.array(item), next_cursor: z.string().nullable() });

/** identity `SessionUser` (C6): login and accept only; never sent to the browser. */
export const SessionUser = z.object({
  user_id: Uuid,
  company_id: Uuid,
  email: z.string(),
  name: z.string(),
  roles: z.array(RoleEnum),
  roles_version: z.number().int(),
  status: z.literal("active"),
});
export type SessionUser = z.infer<typeof SessionUser>;

const UserStatus = z.enum(["pending", "active", "inactive"]);

/** identity `UserOut` (C11). */
export const User = z.object({
  id: Uuid,
  email: z.string(),
  name: z.string(),
  roles: z.array(RoleEnum),
  roles_version: z.number().int(),
  status: UserStatus,
  created_at: DateTime,
  updated_at: DateTime,
  activated_at: DateTime.nullable(),
  deactivated_at: DateTime.nullable(),
  pending_invite_id: Uuid.nullable(),
});
export type User = z.infer<typeof User>;

/** identity `MeOut` (R19): the re-check. */
export const UserMe = User.extend({ company_id: Uuid });
export type UserMe = z.infer<typeof UserMe>;

/** BFF only (PAY-104, 106, 107). */
export const Me = z.object({
  id: Uuid,
  email: z.string(),
  name: z.string(),
  roles: z.array(RoleEnum),
  roles_version: z.number().int(),
  status: UserStatus,
});
export type Me = z.infer<typeof Me>;

/** `Me` from a `SessionUser` (id = user_id; company_id stays in the session as cid, C6) or a `UserMe`. */
export function toMe(u: SessionUser | UserMe): Me {
  return {
    id: "user_id" in u ? u.user_id : u.id,
    email: u.email,
    name: u.name,
    roles: u.roles,
    roles_version: u.roles_version,
    status: u.status,
  };
}

/** identity `PendingInviteOut` (resend response; R17, C31): `expires_at` is null until sent. */
export const PendingInvite = z.object({
  id: Uuid,
  expires_at: DateTime.nullable(),
  email_status: z.enum(["pending", "sent", "failed", "cancelled"]).nullable(),
});
export type PendingInvite = z.infer<typeof PendingInvite>;

/** identity `ResetConfirmOut` (R19): BFF only; drives the session revocation. */
export const ResetConfirmResult = z.object({ user_id: Uuid, roles_version: z.number().int() });
export type ResetConfirmResult = z.infer<typeof ResetConfirmResult>;

/** identity `AuditEntryOut` (PAY-109). */
export const AuthAuditEvent = z.object({
  id: Uuid,
  at: DateTime,
  actor_user_id: Uuid.nullable(),
  event: z.string(),
  target_user_id: Uuid.nullable(),
  ip_hash: z.string().nullable(),
  details: z.record(z.string(), z.unknown()),
});
export type AuthAuditEvent = z.infer<typeof AuthAuditEvent>;

/** payroll `AuditEventOut` (C5). */
export const PayrollAuditEvent = z.object({
  id: Uuid,
  at: DateTime,
  actor_id: z.string(),
  event: z.string(),
  entity_type: z.enum(["company_settings", "employee", "pay_run", "payout"]),
  entity_id: Uuid,
  details: z.record(z.string(), z.unknown()),
});
export type PayrollAuditEvent = z.infer<typeof PayrollAuditEvent>;

/** payroll `CompanySettingsOut` (PAY-200). */
export const Settings = z.object({
  name: z.string(),
  tax_rate_bps: z.number().int(),
  retirement_bps: z.number().int(),
  timezone: z.string(),
  biweekly_anchor: IsoDate,
  require_dual_approval: z.boolean(),
  updated_at: DateTime,
  updated_by: z.string().nullable(),
});
export type Settings = z.infer<typeof Settings>;

/** payroll `EmployeeOut`, strict (C5). */
export const Employee = z.strictObject({
  id: Uuid,
  name: z.string(),
  email: z.string(),
  pay_type: PayType,
  annual_salary_cents: z.number().int().nullable(),
  hourly_rate_cents: z.number().int().nullable(),
  schedule: Schedule,
  account_last4: z
    .string()
    .regex(/^\d{4}$/)
    .nullable(),
  has_bank_details: z.boolean(),
  bank_updated_at: DateTime.nullable(),
  bank_updated_by: z.string().nullable(),
  status: z.enum(["active", "inactive"]),
  deactivated_at: DateTime.nullable(),
  created_at: DateTime,
  updated_at: DateTime,
});
export type Employee = z.infer<typeof Employee>;

/** payroll `RunTotalsOut`. */
export const RunTotals = z.object({
  gross_cents: Cents,
  tax_cents: Cents,
  retirement_cents: Cents,
  net_cents: Cents,
  funding_required_cents: Cents,
});

const Count = z.number().int().min(0);
/** payroll `PayoutCounts` (C9). */
export const RunCounts = z.object({
  requested: Count,
  awaiting_bank: Count,
  blocked: Count,
  paid: Count,
  no_transfer: Count,
  failed: Count,
});
export type RunCounts = z.infer<typeof RunCounts>;

export const PayRunStatusEnum = z.enum([
  "draft",
  "calculated",
  "processing",
  "paid",
  "partially_failed",
]);
export type PayRunStatus = z.infer<typeof PayRunStatusEnum>;

/** payroll `PayRunOut` (R21, C9). */
export const PayRun = z.object({
  id: Uuid,
  schedule: Schedule,
  period_start: IsoDate,
  period_end: IsoDate,
  pay_date: IsoDate,
  period_index: z.number().int(),
  periods_per_year: z.number().int(),
  status: PayRunStatusEnum,
  employee_count: z.number().int(),
  tax_bps: z.number().int().nullable(),
  retirement_bps: z.number().int().nullable(),
  require_dual_approval: z.boolean().nullable(),
  totals: RunTotals.nullable(),
  counts: RunCounts,
  created_by: z.string(),
  created_at: DateTime,
  calculated_by: z.string().nullable(),
  calculated_by_name: z.string().nullable(),
  calculated_at: DateTime.nullable(),
  approved_by: z.string().nullable(),
  approved_by_name: z.string().nullable(),
  approved_at: DateTime.nullable(),
  processing_since: DateTime.nullable(),
  updated_at: DateTime,
});
export type PayRun = z.infer<typeof PayRun>;

/** payroll `HoursEntry`. */
export const HoursEntry = z.object({ employee_id: Uuid, hours_hundredths: z.number().int() });

/** payroll `HoursOut` (C7): every hours row now stored for the run; the run is back in draft. */
export const Hours = z.object({
  pay_run_id: Uuid,
  status: z.literal("draft"),
  entries: z.array(HoursEntry),
});
export type Hours = z.infer<typeof Hours>;

/** payroll `PayslipOut`: no `schedule`; amounts null while pending (C5). */
export const Payslip = z.object({
  id: Uuid,
  pay_run_id: Uuid,
  employee_id: Uuid,
  employee_name: z.string(),
  pay_type: PayType,
  annual_salary_cents: z.number().int().nullable(),
  hourly_rate_cents: z.number().int().nullable(),
  hours_hundredths: z.number().int().nullable(),
  status: z.enum(["pending", "calculated"]),
  gross_cents: Cents.nullable(),
  tax_cents: Cents.nullable(),
  retirement_cents: Cents.nullable(),
  net_cents: Cents.nullable(),
});
export type Payslip = z.infer<typeof Payslip>;

export const AttemptStatus = z.enum([
  "requested",
  "indeterminate",
  "needs_operator",
  "settled",
  "rejected",
]);

/** payroll `AttemptOut`, strict (C5, C26). */
export const Attempt = z.strictObject({
  attempt: z.number().int().min(1),
  status: AttemptStatus,
  failure_reason: z.string().nullable(),
  bank_reference: z.string().max(64).nullable(),
  account_last4: z.string().regex(/^\d{4}$/),
  requested_at: DateTime,
  final_at: DateTime.nullable(),
});
export type Attempt = z.infer<typeof Attempt>;

/** payroll `PayoutOut`, strict (R21, C5). */
export const Payout = z.strictObject({
  id: Uuid,
  pay_run_id: Uuid,
  payslip_id: Uuid,
  employee_id: Uuid,
  employee_name: z.string(),
  amount_cents: Cents,
  currency: z.literal("USD"),
  status: z.enum(["requested", "paid", "failed"]),
  no_transfer: z.boolean(),
  current_attempt: z.number().int().min(0),
  attempts: z.array(Attempt),
});
export type Payout = z.infer<typeof Payout>;

/** The PAY-602 error body. */
export const ErrorBody = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string(),
    details: z.unknown().optional(),
  }),
});
