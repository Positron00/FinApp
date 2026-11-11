import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { IdParam } from "@/lib/schemas/requests";
import { PayRun } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs/[id]/approve";
const ALLOWED = ["POST"];

/**
 * POST /api/pay-runs/[id]/approve (PAY-400, PAY-401): approver. `Idempotency-Key` is required
 * (missing → 400, not a UUID → 422; C34) and forwarded unchanged. Read timeout 10 s; never
 * retried by the BFF.
 */
export const POST = route({
  name: NAME,
  auth: { roles: ["approver"] },
  params: IdParam,
  idempotencyKey: true,
  async handler({ params, payroll, slowFetch, idempotencyKey }) {
    const run = await callService(
      "payroll",
      NAME,
      payroll.POST("/v1/pay-runs/{run_id}/approve", {
        params: {
          path: { run_id: params.id },
          header: { "Idempotency-Key": idempotencyKey },
        },
        fetch: slowFetch,
      }),
      PayRun,
    );
    return json(run);
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
