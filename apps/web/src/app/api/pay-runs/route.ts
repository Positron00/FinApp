import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { PayRunCreateBody, PayRunListQuery } from "@/lib/schemas/requests";
import { Page, PayRun } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs";
const ALLOWED = ["GET", "POST"];

/** GET /api/pay-runs (PAY-303): any role; status, cursor and limit pass through (C8). */
export const GET = route({
  name: NAME,
  auth: {},
  query: PayRunListQuery,
  async handler({ query, payroll }) {
    const page = await callService(
      "payroll",
      NAME,
      payroll.GET("/v1/pay-runs", { params: { query } }),
      Page(PayRun),
    );
    return json(page);
  },
});

/** POST /api/pay-runs (PAY-300): payroll_admin. */
export const POST = route({
  name: NAME,
  auth: { roles: ["payroll_admin"] },
  body: PayRunCreateBody,
  async handler({ body, payroll }) {
    const run = await callService("payroll", NAME, payroll.POST("/v1/pay-runs", { body }), PayRun, {
      schedule: body.schedule,
    });
    return json(run, 201);
  },
});

export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
