import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { EmployeeCreateBody, EmployeeListQuery } from "@/lib/schemas/requests";
import { Employee, Page } from "@/lib/schemas/responses";

const NAME = "/api/employees";
const ALLOWED = ["GET", "POST"];

/**
 * GET /api/employees (PAY-202): any role; status, cursor and limit pass through (C8).
 * `Employee` is strict, so a response with a full bank number fails parsing: 500 plus
 * `bff.response_invalid`, and nothing leaks (PAY-NFR-08).
 */
export const GET = route({
  name: NAME,
  auth: {},
  query: EmployeeListQuery,
  async handler({ query, payroll }) {
    const page = await callService(
      "payroll",
      NAME,
      payroll.GET("/v1/employees", { params: { query } }),
      Page(Employee),
    );
    return json(page);
  },
});

/** POST /api/employees (PAY-201): payroll_admin. 409 `employee_email_taken`. */
export const POST = route({
  name: NAME,
  auth: { roles: ["payroll_admin"] },
  body: EmployeeCreateBody,
  async handler({ body, payroll }) {
    const employee = await callService(
      "payroll",
      NAME,
      payroll.POST("/v1/employees", { body }),
      Employee,
    );
    return json(employee, 201);
  },
});

export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
