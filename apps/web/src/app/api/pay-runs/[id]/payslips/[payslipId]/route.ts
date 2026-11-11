import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { RunPayslipParam } from "@/lib/schemas/requests";
import { Payslip } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs/[id]/payslips/[payslipId]";
const ALLOWED = ["GET"];

/**
 * GET /api/pay-runs/[id]/payslips/[payslipId] (PAY-303): any role. 404 `pay_run_not_found` or
 * `payslip_not_found`.
 */
export const GET = route({
  name: NAME,
  auth: {},
  params: RunPayslipParam,
  async handler({ params, payroll }) {
    const payslip = await callService(
      "payroll",
      NAME,
      payroll.GET("/v1/pay-runs/{run_id}/payslips/{payslip_id}", {
        params: { path: { run_id: params.id, payslip_id: params.payslipId } },
      }),
      Payslip,
    );
    return json(payslip);
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
