import { callServiceNoContent } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { ResetRequestBody } from "@/lib/schemas/requests";

const NAME = "/api/auth/reset/request";
const ALLOWED = ["POST"];

/**
 * POST /api/auth/reset/request (PAY-105): public; the IP limiter fails closed. identity answers
 * 202 whether or not the account exists (no enumeration; C68), and so does the BFF: `{}`.
 */
export const POST = route({
  name: NAME,
  auth: "public",
  publicLimiter: "fail_closed",
  body: ResetRequestBody,
  async handler({ body, identity }) {
    await callServiceNoContent(
      "identity",
      NAME,
      identity.POST("/v1/auth/password-reset/request", { body }),
    );
    return json({}, 202);
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
