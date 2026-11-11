import { json, methodNotAllowed, route } from "@/lib/route";

const NAME = "/api/livez";
const ALLOWED = ["GET"];

/** GET /api/livez (PAY-605, C4): liveness, no dependencies and no rate limit. */
export const GET = route({
  name: NAME,
  auth: "public",
  ipLimit: "none",
  quiet: true,
  async handler() {
    return json({ status: "ok" });
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
