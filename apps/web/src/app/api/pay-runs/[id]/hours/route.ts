import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { HoursBody, IdParam } from "@/lib/schemas/requests";
import { Hours } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs/[id]/hours";
const ALLOWED = ["PUT"];

/**
 * PUT /api/pay-runs/[id]/hours (PAY-301): payroll_admin. Returns payroll's `HoursOut` (C7); the
 * run is back in `draft`, and the UI refetches the run and its payslips. 422 `not_hourly` and
 * `employee_not_in_run` forward `details.employee_ids` (C15); 409 `pay_run_locked`.
 */
export const PUT = route({
  name: NAME,
  auth: { roles: ["payroll_admin"] },
  params: IdParam,
  body: HoursBody,
  async handler({ params, body, payroll }) {
    const hours = await callService(
      "payroll",
      NAME,
      payroll.PUT("/v1/pay-runs/{run_id}/hours", {
        params: { path: { run_id: params.id } },
        body,
      }),
      Hours,
    );
    return json(hours);
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const POST = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
