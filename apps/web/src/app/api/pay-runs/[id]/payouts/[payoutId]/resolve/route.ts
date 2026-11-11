import { callService } from "@/lib/api/call";
import { BffError } from "@/lib/errors";
import { json, methodNotAllowed, route } from "@/lib/route";
import { ResolveBody, RunPayoutParam } from "@/lib/schemas/requests";
import { Payout } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs/[id]/payouts/[payoutId]/resolve";
const ALLOWED = ["POST"];

/**
 * POST /api/pay-runs/[id]/payouts/[payoutId]/resolve (PAY-502, R8): approver. `{action, note}`
 * is forwarded unchanged; the call is never retried (POST). 409 `payout_not_blocked`; 409
 * `resolve_too_early` with `Retry-After` forwarded (C48). The note is never logged:
 * `bff.payout_resolve` carries its length only (PAY-NFR-07).
 */
export const POST = route({
  name: NAME,
  auth: { roles: ["approver"] },
  params: RunPayoutParam,
  body: ResolveBody,
  async handler({ params, body, payroll, log }) {
    let status = 200;
    try {
      const payout = await callService(
        "payroll",
        NAME,
        payroll.POST("/v1/pay-runs/{run_id}/payouts/{payout_id}/resolve", {
          params: { path: { run_id: params.id, payout_id: params.payoutId } },
          body,
        }),
        Payout,
      );
      return json(payout);
    } catch (err) {
      status = err instanceof BffError ? err.status : 500;
      throw err;
    } finally {
      log.info(
        { event: "bff.payout_resolve", action: body.action, note_len: body.note.length, status },
        "payout resolve",
      );
    }
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
