import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { IdParam, ListQuery } from "@/lib/schemas/requests";
import { Page, Payslip } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs/[id]/payslips";
const ALLOWED = ["GET"];

/** GET /api/pay-runs/[id]/payslips (PAY-303): any role; cursor and limit pass through (C8). */
export const GET = route({
  name: NAME,
  auth: {},
  params: IdParam,
  query: ListQuery,
  async handler({ params, query, payroll }) {
    const page = await callService(
      "payroll",
      NAME,
      payroll.GET("/v1/pay-runs/{run_id}/payslips", {
        params: { path: { run_id: params.id }, query },
      }),
      Page(Payslip),
    );
    return json(page);
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
