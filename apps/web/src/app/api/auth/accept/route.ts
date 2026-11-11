import { NextResponse } from "next/server";

import { callService } from "@/lib/api/call";
import { config } from "@/lib/config";
import { methodNotAllowed, route } from "@/lib/route";
import { AcceptBody } from "@/lib/schemas/requests";
import { SessionUser, toMe } from "@/lib/schemas/responses";
import { cookieOptions, createSession } from "@/lib/session";

const NAME = "/api/auth/accept";
const ALLOWED = ["POST"];

/**
 * POST /api/auth/accept (PAY-103, PAY-106): public; the IP limiter fails closed. identity
 * accepts the invite with a `service:web` token (R9) and `X-Client-IP` when known (C16), and
 * returns a `SessionUser` (C6); the BFF then creates a fresh session, as on sign-in. The token
 * travels only in the body, never in a URL or a log (PAY-NFR-07).
 */
export const POST = route({
  name: NAME,
  auth: "public",
  publicLimiter: "fail_closed",
  body: AcceptBody,
  async handler({ body, req, identity }) {
    const user = await callService(
      "identity",
      NAME,
      identity.POST("/v1/invites/accept", { body }),
      SessionUser,
    );
    const c = config();
    const id = await createSession(
      {
        user_id: user.user_id,
        company_id: user.company_id,
        name: user.name,
        roles: user.roles,
        roles_version: user.roles_version,
      },
      "accept",
      req.cookies.get(c.SESSION_COOKIE_NAME)?.value,
    );
    const res = NextResponse.json({ user: toMe(user) });
    res.cookies.set(c.SESSION_COOKIE_NAME, id, cookieOptions());
    return res;
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
