import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { ListQuery } from "@/lib/schemas/requests";
import { Page, User } from "@/lib/schemas/responses";

const NAME = "/api/users";
const ALLOWED = ["GET"];

/**
 * GET /api/users (PAY-108): admin. cursor and limit pass through (C8); each pending user carries
 * `pending_invite_id` for Resend (R21, C11).
 */
export const GET = route({
  name: NAME,
  auth: { roles: ["admin"] },
  query: ListQuery,
  async handler({ query, identity }) {
    const page = await callService(
      "identity",
      NAME,
      identity.GET("/v1/users", { params: { query } }),
      Page(User),
    );
    return json(page);
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
