import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { IdParam } from "@/lib/schemas/requests";
import { PayRun } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs/[id]";
const ALLOWED = ["GET"];

/**
 * GET /api/pay-runs/[id] (PAY-303, PAY-413): any role. Polled every 2 s while `processing`;
 * `counts` carries every number the run header and banner need (R21, C9).
 */
export const GET = route({
  name: NAME,
  auth: {},
  params: IdParam,
  async handler({ params, payroll }) {
    const run = await callService(
      "payroll",
      NAME,
      payroll.GET("/v1/pay-runs/{run_id}", { params: { path: { run_id: params.id } } }),
      PayRun,
    );
    return json(run);
  },
});

// DELETE arrives with F3/F5 (payroll has no delete endpoint yet).
export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
