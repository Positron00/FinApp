import "server-only";

import { config } from "./config";
import { bffError } from "./errors";
import { logger } from "./logger";

export const MUTATIONS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * The CSRF defence with SameSite=Lax (web LLD §5.4, PAY-604): a mutation's `Origin` must equal
 * PUBLIC_BASE_URL's origin exactly. Missing, `null` or any other value → 403 `bad_origin`.
 */
export function checkOrigin(req: Request, route: string): void {
  if (!MUTATIONS.has(req.method)) return;
  const expected = new URL(config().PUBLIC_BASE_URL).origin;
  const origin = req.headers.get("origin");
  if (origin === expected) return;
  let originHost: string | null = null;
  try {
    originHost = origin && origin !== "null" ? new URL(origin).host : null;
  } catch {
    originHost = null;
  }
  logger().warn({ event: "security.bad_origin", route, origin_host: originHost }, "bad origin");
  throw bffError("bad_origin");
}
