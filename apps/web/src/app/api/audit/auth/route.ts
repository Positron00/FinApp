import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { ListQuery } from "@/lib/schemas/requests";
import { AuthAuditEvent, Page } from "@/lib/schemas/responses";

const NAME = "/api/audit/auth";
const ALLOWED = ["GET"];

/** GET /api/audit/auth (PAY-109): admin; newest first; cursor and limit pass through (C8). */
export const GET = route({
  name: NAME,
  auth: { roles: ["admin"] },
  query: ListQuery,
  async handler({ query, identity }) {
    const page = await callService(
      "identity",
      NAME,
      identity.GET("/v1/audit-log", { params: { query } }),
      Page(AuthAuditEvent),
    );
    return json(page);
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
