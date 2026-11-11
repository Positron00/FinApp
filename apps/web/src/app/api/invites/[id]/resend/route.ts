import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { IdParam } from "@/lib/schemas/requests";
import { PendingInvite } from "@/lib/schemas/responses";

const NAME = "/api/invites/[id]/resend";
const ALLOWED = ["POST"];

/**
 * POST /api/invites/[id]/resend (PAY-102): admin. `[id]` is the user's `pending_invite_id`
 * (R21, C11). 200 `PendingInvite`; 409 `invite_not_pending`; 404 `invite_not_found`.
 */
export const POST = route({
  name: NAME,
  auth: { roles: ["admin"] },
  params: IdParam,
  async handler({ params, identity }) {
    const invite = await callService(
      "identity",
      NAME,
      identity.POST("/v1/invites/{id}/resend", { params: { path: { id: params.id } } }),
      PendingInvite,
    );
    return json(invite);
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
