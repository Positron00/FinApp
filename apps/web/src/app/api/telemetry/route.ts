import { NextResponse } from "next/server";

import { config } from "@/lib/config";
import { logger } from "@/lib/logger";
import { methodNotAllowed, route } from "@/lib/route";
import { TelemetryBody } from "@/lib/schemas/requests";

const NAME = "/api/telemetry";
const ALLOWED = ["POST"];

/**
 * POST /api/telemetry (web LLD §7.5): Web Vitals and browser errors. Optional session, Origin
 * check, an 8 KB body and its own per-IP limit (failing open). User-entered text (an error
 * message) is logged as its length outside dev (PAY-NFR-07).
 */
export const POST = route({
  name: NAME,
  auth: "optional",
  ipLimit: "telemetry",
  body: TelemetryBody,
  bodyLimit: 8192,
  async handler({ body }) {
    if (body.type === "vital") {
      logger().info(
        {
          event: "web.vitals",
          vital: body.name,
          value: body.value,
          rating: body.rating,
          page: body.route,
        },
        "web vital",
      );
    } else {
      const dev = config().ENV === "dev";
      logger().warn(
        {
          event: "web.client_error",
          page: body.route,
          ...(dev ? { message: body.message } : { message_len: body.message.length }),
        },
        "browser error",
      );
    }
    return new NextResponse(null, { status: 204 });
  },
});

export const GET = methodNotAllowed(NAME, ALLOWED);
export const PUT = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
