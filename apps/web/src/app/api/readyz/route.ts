import { bffError } from "@/lib/errors";
import { redis } from "@/lib/redis";
import { json, methodNotAllowed, route } from "@/lib/route";

const NAME = "/api/readyz";
const ALLOWED = ["GET"];

/**
 * GET /api/readyz (PAY-605, C4): readiness is Redis `PING` within REDIS_SOCKET_TIMEOUT_MS
 * (the ioredis command timeout). 503 `service_unavailable` when Redis is down.
 */
export const GET = route({
  name: NAME,
  auth: "public",
  ipLimit: "none",
  quiet: true,
  async handler() {
    try {
      await redis().ping();
    } catch {
      throw bffError("service_unavailable");
    }
    return json({ status: "ok" });
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
