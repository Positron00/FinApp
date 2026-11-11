import { z } from "zod";

import { abaChecksumOk } from "../aba";

import {
  Cursor,
  Email,
  IsoDate,
  Limit,
  Password,
  PayType,
  RoleEnum,
  Schedule,
  Uuid,
} from "./common";

/**
 * Request schemas (web LLD §2.3), shared with the forms. Every object is strict: an unknown key
 * is a 422 `validation_error`, whose details never echo the submitted value (C14).
 */

const unique = <T>(a: T[]) => new Set(a).size === a.length;
const atLeastOneField = (o: object) => Object.values(o).some((v) => v !== undefined);

/** PAY-104: no minimum length at sign-in. */
export const LoginBody = z.strictObject({
  email: Email,
  password: z.string().min(1).max(256),
});
export type LoginBody = z.infer<typeof LoginBody>;

/** Invite and reset tokens: base64url, never in a URL query or a log (PAY-103, PAY-105). */
export const Token = z.string().regex(/^[A-Za-z0-9_-]{20,128}$/);

/** PAY-103. */
export const AcceptBody = z.strictObject({ token: Token, password: Password });
export type AcceptBody = z.infer<typeof AcceptBody>;

/** PAY-105. */
export const ResetRequestBody = z.strictObject({ email: Email });
export type ResetRequestBody = z.infer<typeof ResetRequestBody>;

/** PAY-105. */
export const ResetConfirmBody = z.strictObject({ token: Token, password: Password });
export type ResetConfirmBody = z.infer<typeof ResetConfirmBody>;

/** 1–3 unique roles (PAY-101, PAY-108). */
export const RoleList = z.array(RoleEnum).min(1).max(3).refine(unique, "duplicate_role");

/** PAY-101. */
export const InviteBody = z.strictObject({
  email: Email,
  name: z.string().trim().min(1).max(200),
  roles: RoleList,
});
export type InviteBody = z.infer<typeof InviteBody>;

/** PAY-108. `status` is "inactive" only: there's no user reactivation in 0.1.0 (R37, C25). */
export const UserPatchBody = z
  .strictObject({ roles: RoleList.optional(), status: z.literal("inactive").optional() })
  .refine(atLeastOneField, "no_fields");
export type UserPatchBody = z.infer<typeof UserPatchBody>;

/** A real calendar date that is a Monday (the biweekly anchor, PAY-200). */
export function isMonday(iso: string): boolean {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return false;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const date = new Date(Date.UTC(y, mo - 1, d));
  return date.getUTCMonth() === mo - 1 && date.getUTCDate() === d && date.getUTCDay() === 1;
}

/** The IANA time zones the runtime knows (plus "UTC", which some runtimes list only as an alias). */
export function timeZones(): string[] {
  const list = Intl.supportedValuesOf("timeZone");
  return list.includes("UTC") ? list : [...list, "UTC"];
}

/** PAY-200. */
export const SettingsBody = z.strictObject({
  name: z.string().trim().min(1).max(200),
  tax_rate_bps: z.number().int().min(0).max(5000),
  retirement_bps: z.number().int().min(0).max(2000),
  timezone: z
    .string()
    .max(64)
    .refine((v) => timeZones().includes(v), "timezone"),
  biweekly_anchor: IsoDate.refine(isMonday, "not_monday"),
  require_dual_approval: z.boolean(),
});
export type SettingsBody = z.infer<typeof SettingsBody>;

/** 9 digits with a valid ABA checksum (payroll is authoritative). */
export const BankRouting = z
  .string()
  .regex(/^\d{9}$/)
  .refine(abaChecksumOk, "aba_checksum");
export const BankAccount = z.string().regex(/^\d{4,17}$/);
const EmployeeName = z.string().trim().min(1).max(200);
const AnnualSalaryCents = z.number().int().min(1).max(1_000_000_000);
const HourlyRateCents = z.number().int().min(1).max(100_000);

const employeeCommon = {
  name: EmployeeName,
  email: Email,
  schedule: Schedule,
  bank_routing: BankRouting,
  bank_account: BankAccount,
};

/** PAY-201: a discriminated union on `pay_type`. */
export const EmployeeCreateBody = z.discriminatedUnion("pay_type", [
  z.strictObject({
    ...employeeCommon,
    pay_type: z.literal("salaried"),
    annual_salary_cents: AnnualSalaryCents,
  }),
  z.strictObject({
    ...employeeCommon,
    pay_type: z.literal("hourly"),
    hourly_rate_cents: HourlyRateCents,
  }),
]);
export type EmployeeCreateBody = z.infer<typeof EmployeeCreateBody>;

/**
 * PAY-203: every create field optional, plus `status`; at least one field. Reactivating an
 * employee whose bank details were purged needs both bank fields in the same PATCH (payroll
 * answers 422 otherwise).
 */
export const EmployeePatchBody = z
  .strictObject({
    name: EmployeeName.optional(),
    email: Email.optional(),
    schedule: Schedule.optional(),
    pay_type: PayType.optional(),
    annual_salary_cents: AnnualSalaryCents.optional(),
    hourly_rate_cents: HourlyRateCents.optional(),
    bank_routing: BankRouting.optional(),
    bank_account: BankAccount.optional(),
    status: z.enum(["active", "inactive"]).optional(),
  })
  .refine(atLeastOneField, "no_fields");
export type EmployeePatchBody = z.infer<typeof EmployeePatchBody>;

/** PAY-301: 1–500 unique entries; the per-schedule bound is payroll's. */
export const HoursBody = z.strictObject({
  entries: z
    .array(
      z.strictObject({
        employee_id: Uuid,
        hours_hundredths: z.number().int().min(0).max(74_400),
      }),
    )
    .min(1)
    .max(500)
    .refine((e) => unique(e.map((x) => x.employee_id)), "duplicate_employee_id"),
});
export type HoursBody = z.infer<typeof HoursBody>;

/** PAY-502: the note is forwarded unchanged and never logged (only its length). */
export const ResolveBody = z.strictObject({
  action: z.enum(["redrive", "mark_rejected"]),
  note: z.string().trim().min(1).max(500),
});
export type ResolveBody = z.infer<typeof ResolveBody>;

/** C8: cursor and limit pass through to the service unchanged. */
export const ListQuery = z.strictObject({ cursor: Cursor, limit: Limit });

/** PAY-202. */
export const EmployeeListQuery = z.strictObject({
  cursor: Cursor,
  limit: Limit,
  status: z.enum(["active", "inactive"]).optional(),
});

export const PayRunStatus = z.enum([
  "draft",
  "calculated",
  "processing",
  "paid",
  "partially_failed",
]);
export const PayRunListQuery = z.strictObject({
  cursor: Cursor,
  limit: Limit,
  status: PayRunStatus.optional(),
});

export const PayoutListQuery = z.strictObject({
  cursor: Cursor,
  limit: Limit,
  status: z.enum(["requested", "paid", "failed"]).optional(),
});

/** PAY-300. */
export const PayRunCreateBody = z.strictObject({ schedule: Schedule, period_start: IsoDate });
export type PayRunCreateBody = z.infer<typeof PayRunCreateBody>;

/** PAY-400, C34: the browser makes one per click with crypto.randomUUID(). */
export const IdempotencyKeyHeader = Uuid;

export const IdParam = z.strictObject({ id: Uuid });
export const RunPayslipParam = z.strictObject({ id: Uuid, payslipId: Uuid });
export const RunPayoutParam = z.strictObject({ id: Uuid, payoutId: Uuid });

/**
 * §7.5: `route` is a page template such as "/pay-runs/[id]" (IDs replaced in the browser),
 * never free text, so nothing user-entered reaches the logs through it (PAY-NFR-07, C63).
 */
export const TelemetryRoute = z
  .string()
  .max(100)
  .regex(/^\/[a-z0-9\-/[\]]*$/);

/** §7.5. */
export const TelemetryBody = z.discriminatedUnion("type", [
  z.strictObject({
    type: z.literal("vital"),
    name: z.enum(["CLS", "FCP", "INP", "LCP", "TTFB"]),
    value: z.number().min(0),
    rating: z.enum(["good", "needs-improvement", "poor"]),
    route: TelemetryRoute,
  }),
  z.strictObject({
    type: z.literal("error"),
    message: z.string().max(500),
    route: TelemetryRoute,
  }),
]);
export type TelemetryBody = z.infer<typeof TelemetryBody>;
