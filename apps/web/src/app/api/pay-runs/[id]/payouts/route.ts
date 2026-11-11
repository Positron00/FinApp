import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { IdParam, PayoutListQuery } from "@/lib/schemas/requests";
import { Page, Payout } from "@/lib/schemas/responses";

const NAME = "/api/pay-runs/[id]/payouts";
const ALLOWED = ["GET"];

/**
 * GET /api/pay-runs/[id]/payouts (PAY-501, PAY-413): any role; status, cursor and limit pass
 * through (C8). `Payout` and `Attempt` are strict, so no full bank number can pass (PAY-NFR-08).
 */
export const GET = route({
  name: NAME,
  auth: {},
  params: IdParam,
  query: PayoutListQuery,
  async handler({ params, query, payroll }) {
    const page = await callService(
      "payroll",
      NAME,
      payroll.GET("/v1/pay-runs/{run_id}/payouts", {
        params: { path: { run_id: params.id }, query },
      }),
      Page(Payout),
    );
    return json(page);
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
