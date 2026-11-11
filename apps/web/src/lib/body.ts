import "server-only";

import type { z } from "zod";

import { config } from "./config";
import { bffError, zodDetails } from "./errors";
import { logger } from "./logger";

/**
 * Size-capped JSON body reading (web LLD §5.3, PAY-604): Content-Length checked first, then the
 * stream is counted, so chunked or understated bodies are capped too.
 */
async function readCapped(req: Request, limit: number, route: string): Promise<Uint8Array> {
  const declared = Number(req.headers.get("content-length") ?? "NaN");
  if (Number.isFinite(declared) && declared > limit) tooLarge(route, declared);
  if (!req.body) return new Uint8Array();
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel().catch(() => undefined);
      tooLarge(route, total);
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.byteLength;
  }
  return out;
}

function tooLarge(route: string, bytes: number): never {
  logger().warn({ event: "security.payload_too_large", route, bytes }, "payload too large");
  throw bffError("payload_too_large");
}

export async function readJson<T>(
  req: Request,
  schema: z.ZodType<T>,
  route: string,
  limit: number = config().BFF_BODY_LIMIT_BYTES,
): Promise<T> {
  const bytes = await readCapped(req, limit, route);
  const ct = req.headers.get("content-type") ?? "";
  if (!/^application\/json\s*(;\s*charset=utf-8\s*)?$/i.test(ct.trim())) {
    // 415 is folded into 422 (§5.3).
    throw bffError("validation_error", {
      details: [{ field: "header.Content-Type", issue: "invalid_type" }],
    });
  }
  let value: unknown;
  try {
    value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw bffError("invalid_json");
  }
  const parsed = schema.safeParse(value);
  if (!parsed.success)
    throw bffError("validation_error", { details: zodDetails("body", parsed.error) });
  return parsed.data;
}

/** For routes without a body schema: a non-empty body is a 422 (§5.3). */
export async function rejectBody(req: Request, route: string): Promise<void> {
  const bytes = await readCapped(req, config().BFF_BODY_LIMIT_BYTES, route);
  if (bytes.byteLength > 0) {
    throw bffError("validation_error", { details: [{ field: "body", issue: "unexpected" }] });
  }
}
