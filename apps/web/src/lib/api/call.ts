import "server-only";

import type { z } from "zod";

import { mapUpstreamError, type MessageVars, type Target } from "../errors";
import { parseUpstream } from "../route";

interface FetchResult {
  data?: unknown;
  error?: unknown;
  response: Response;
}

/**
 * Awaits an openapi-fetch call: a non-2xx response is mapped through the error catalogue
 * (§3.3); a 2xx body is parsed with its §2.4 schema (500 + `bff.response_invalid` on failure).
 */
export async function callService<T>(
  target: Target,
  routeName: string,
  call: Promise<FetchResult>,
  schema: z.ZodType<T>,
  vars: MessageVars = {},
): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok) {
    throw mapUpstreamError(
      target,
      { status: response.status, body: error, headers: response.headers },
      routeName,
      vars,
    );
  }
  return parseUpstream(schema, data, target, routeName);
}
