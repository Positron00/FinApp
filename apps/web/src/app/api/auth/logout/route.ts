import { NextResponse } from "next/server";

import { config } from "@/lib/config";
import { logger } from "@/lib/logger";
import { methodNotAllowed, route } from "@/lib/route";
import { cookieOptions, deleteSessionByKey, sessionKeyOf } from "@/lib/session";

const NAME = "/api/auth/logout";
const ALLOWED = ["POST"];

/**
 * POST /api/auth/logout (PAY-106): deletes this session (DEL + SREM) and clears the cookie.
 * Idempotent without a session; a Redis error is logged and the response is still 204 (§4.5).
 */
export const POST = route({
  name: NAME,
  auth: "optional",
  async handler({ req }) {
    const c = config();
    const cookie = req.cookies.get(c.SESSION_COOKIE_NAME)?.value;
    if (cookie) {
      try {
        await deleteSessionByKey(sessionKeyOf(cookie));
      } catch (err) {
        logger().error(
          { event: "session.backend_error", op: "logout", err: String(err) },
          "logout failed",
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
