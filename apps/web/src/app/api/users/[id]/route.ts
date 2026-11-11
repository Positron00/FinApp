import { callService } from "@/lib/api/call";
import { BffError } from "@/lib/errors";
import { json, methodNotAllowed, route } from "@/lib/route";
import { IdParam, UserPatchBody } from "@/lib/schemas/requests";
import { User } from "@/lib/schemas/responses";
import { revokeUserSessions } from "@/lib/session";

const NAME = "/api/users/[id]";
const ALLOWED = ["PATCH"];

/**
 * PATCH /api/users/[id] (PAY-107, PAY-108): admin. `status` may only be "inactive" (no user
 * reactivation in 0.1.0; R37, C25). On identity's 200 every session of the target user is
 * revoked at once, so their next request is 401 without waiting for the 60 s re-check (§4.4).
 *
 * If Redis fails during the revoke, identity has already applied the change: the route still
 * returns the 200, `auth.session.revoke_failed` is logged, and the re-check (identity bumped
 * `roles_version`) ends the sessions within 60 s once Redis is back.
 */
export const PATCH = route({
  name: NAME,
  auth: { roles: ["admin"] },
  params: IdParam,
  body: UserPatchBody,
  async handler({ params, body, identity }) {
    const user = await callService(
      "identity",
      NAME,
      identity.PATCH("/v1/users/{id}", { params: { path: { id: params.id } }, body }),
      User,
    );
    try {
      await revokeUserSessions(
        params.id,
        body.status === "inactive" ? "deactivated" : "role_change",
      );
    } catch (err) {
      if (!(err instanceof BffError)) throw err; // revoke_failed is logged by revokeUserSessions
    }
    return json(user);
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
