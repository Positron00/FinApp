import { z } from "zod";

import { Cursor, Email, IsoDate, Limit, Schedule, Uuid } from "./common";

/**
 * Request schemas (web LLD §2.3). Every object is strict: an unknown key is a 422
 * `validation_error`. S7 has the skeleton journey's schemas; F5 adds the rest.
 */

/** PAY-104: no minimum length at sign-in. */
export const LoginBody = z.strictObject({
  email: Email,
  password: z.string().min(1).max(256),
});
export type LoginBody = z.infer<typeof LoginBody>;

/** C8: cursor and limit pass through to the service unchanged. */
export const ListQuery = z.strictObject({ cursor: Cursor, limit: Limit });

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
