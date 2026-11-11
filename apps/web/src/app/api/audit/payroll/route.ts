import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { ListQuery } from "@/lib/schemas/requests";
import { Page, PayrollAuditEvent } from "@/lib/schemas/responses";

const NAME = "/api/audit/payroll";
const ALLOWED = ["GET"];

/** GET /api/audit/payroll (plan §8): admin; newest first; cursor and limit pass through (C8). */
export const GET = route({
  name: NAME,
  auth: { roles: ["admin"] },
  query: ListQuery,
  async handler({ query, payroll }) {
    const page = await callService(
      "payroll",
      NAME,
      payroll.GET("/v1/audit-events", { params: { query } }),
      Page(PayrollAuditEvent),
    );
    return json(page);
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
