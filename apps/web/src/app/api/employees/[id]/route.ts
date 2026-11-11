import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { EmployeePatchBody, IdParam } from "@/lib/schemas/requests";
import { Employee } from "@/lib/schemas/responses";

const NAME = "/api/employees/[id]";
const ALLOWED = ["GET", "PATCH"];

/** GET /api/employees/[id] (PAY-202): any role. 404 `employee_not_found`. */
export const GET = route({
  name: NAME,
  auth: {},
  params: IdParam,
  async handler({ params, payroll }) {
    const employee = await callService(
      "payroll",
      NAME,
      payroll.GET("/v1/employees/{employee_id}", {
        params: { path: { employee_id: params.id } },
      }),
      Employee,
    );
    return json(employee);
  },
});

/**
 * PATCH /api/employees/[id] (PAY-203, PAY-608): payroll_admin. Deactivate and reactivate go
 * through `status`; reactivating an employee whose bank details were purged needs both bank
 * fields in the same PATCH, or payroll answers 422 `validation_error`.
 */
export const PATCH = route({
  name: NAME,
  auth: { roles: ["payroll_admin"] },
  params: IdParam,
  body: EmployeePatchBody,
  async handler({ params, body, payroll }) {
    const employee = await callService(
      "payroll",
      NAME,
      payroll.PATCH("/v1/employees/{employee_id}", {
        params: { path: { employee_id: params.id } },
        body,
      }),
      Employee,
    );
    return json(employee);
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
