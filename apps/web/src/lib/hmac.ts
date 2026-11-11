import "server-only";

import { createHmac } from "node:crypto";

import { config } from "./config";

/** HMAC-SHA256(SESSION_SECRET, value) as hex. */
export function hmacHex(value: string): string {
  return createHmac("sha256", config().SESSION_SECRET).update(value).digest("hex");
}

/** 16 hex chars of the HMAC: limiter subjects and `user_id_hash` (web LLD §5.1, §7.1). */
export function hmac16(value: string): string {
  return hmacHex(value).slice(0, 16);
}

export function userIdHash(userId: string): string {
  return hmac16(userId);
}
