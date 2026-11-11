import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { IdParam } from "@/lib/schemas/requests";
import { PayRun } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs/[id]/calculate";
const ALLOWED = ["POST"];

/** POST /api/pay-runs/[id]/calculate (PAY-302): payroll_admin; read timeout 10 s; never retried. */
export const POST = route({
  name: NAME,
  auth: { roles: ["payroll_admin"] },
  params: IdParam,
  async handler({ params, payroll, slowFetch }) {
    const run = await callService(
      "payroll",
      NAME,
      payroll.POST("/v1/pay-runs/{run_id}/calculate", {
        params: { path: { run_id: params.id } },
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
