import { NextResponse } from "next/server";

import { callService } from "@/lib/api/call";
import { config } from "@/lib/config";
import { BffError } from "@/lib/errors";
import { logger } from "@/lib/logger";
import { methodNotAllowed, route } from "@/lib/route";
import { ResetConfirmBody } from "@/lib/schemas/requests";
import { ResetConfirmResult } from "@/lib/schemas/responses";
import { cookieOptions, deleteSessionByKey, revokeUserSessions, sessionKeyOf } from "@/lib/session";

const NAME = "/api/auth/reset/confirm";
const ALLOWED = ["POST"];

/**
 * POST /api/auth/reset/confirm (PAY-105, PAY-107, R19): public; the IP limiter fails closed.
 * On identity's 200 the BFF at once deletes every session in `web:user-sess:<user_id>`, so every
 * device signs in again with the new password, then answers 204 and clears this cookie.
 *
 * The password has changed by then, so a Redis error during the revoke doesn't fail the
 * request: it's logged (`auth.session.revoke_failed`), and identity's `roles_version` bump ends
 * the sessions at the next re-check (§4.3 step 5, within 60 s).
 */
export const POST = route({
  name: NAME,
  auth: "public",
  publicLimiter: "fail_closed",
  body: ResetConfirmBody,
  async handler({ body, req, identity }) {
    const result = await callService(
      "identity",
      NAME,
      identity.POST("/v1/auth/password-reset/confirm", { body }),
      ResetConfirmResult,
    );
    try {
      await revokeUserSessions(result.user_id, "password_reset");
    } catch (err) {
      if (!(err instanceof BffError)) throw err; // revoke_failed is logged by revokeUserSessions
    }
    const c = config();
    const cookie = req.cookies.get(c.SESSION_COOKIE_NAME)?.value;
    if (cookie) {
      try {
        await deleteSessionByKey(sessionKeyOf(cookie));
      } catch (err) {
        logger().warn(
          { event: "session.backend_error", op: "reset_confirm", err: String(err) },
          "could not delete the current session",
        );
      }
    }
    const res = new NextResponse(null, { status: 204 });
    res.cookies.set(c.SESSION_COOKIE_NAME, "", cookieOptions(0));
    return res;
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
