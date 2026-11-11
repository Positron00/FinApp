import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { InviteBody } from "@/lib/schemas/requests";
import { User } from "@/lib/schemas/responses";

const NAME = "/api/users/invite";
const ALLOWED = ["POST"];

/**
 * POST /api/users/invite (PAY-101): admin. 201 with the `pending` user and its
 * `pending_invite_id`; 409 `user_email_taken`; 422 for unknown, empty or duplicate roles.
 */
export const POST = route({
  name: NAME,
  auth: { roles: ["admin"] },
  body: InviteBody,
  async handler({ body, identity }) {
    const user = await callService(
      "identity",
      NAME,
      identity.POST("/v1/users/invite", { body }),
      User,
    );
    return json(user, 201);
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
