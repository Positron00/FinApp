import { bffError } from "@/lib/errors";
import { json, methodNotAllowed, route } from "@/lib/route";
import { recheckNow } from "@/lib/require-session";
import { toMe } from "@/lib/schemas/responses";

const NAME = "/api/auth/me";
const ALLOWED = ["GET"];

/**
 * GET /api/auth/me (PAY-106, PAY-107): any role. A forced re-check against identity
 * `GET /v1/users/me`, so the §4.3 rules apply and the session's roles and name are refreshed.
 */
export const GET = route({
  name: NAME,
  auth: {},
  async handler({ session, ip }) {
    const me = session ? await recheckNow(session, ip) : undefined;
    // A deferred re-check (identity down, inside the grace window) has no fresh profile, and
    // the session doesn't hold the email, so this route answers 503.
    if (!me) throw bffError("service_unavailable");
    return json(toMe(me));
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
