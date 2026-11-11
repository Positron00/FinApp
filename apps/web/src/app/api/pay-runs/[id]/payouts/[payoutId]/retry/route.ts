import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { RunPayoutParam } from "@/lib/schemas/requests";
import { Payout } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs/[id]/payouts/[payoutId]/retry";
const ALLOWED = ["POST"];

/**
 * POST /api/pay-runs/[id]/payouts/[payoutId]/retry (PAY-500): approver. `Idempotency-Key` is a
 * UUID per click, as for approve (C52): missing → 400 `idempotency_key_required`, not a UUID →
 * 422 (C34). It's forwarded unchanged, and the call is never retried by the BFF (POST).
 * `approver_must_differ` and `employee_bank_details_missing` get the retry messages (§3.3).
 */
export const POST = route({
  name: NAME,
  auth: { roles: ["approver"] },
  params: RunPayoutParam,
  idempotencyKey: true,
  async handler({ params, payroll, idempotencyKey }) {
    const payout = await callService(
      "payroll",
      NAME,
      payroll.POST("/v1/pay-runs/{run_id}/payouts/{payout_id}/retry", {
        params: {
          path: { run_id: params.id, payout_id: params.payoutId },
          header: { "Idempotency-Key": idempotencyKey },
        },
      }),
      Payout,
    );
    return json(payout);
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
