import "server-only";

import { isIP } from "node:net";

import { logger } from "./logger";

/**
 * The client IP from `X-Forwarded-For` and TRUSTED_PROXY_HOPS (web LLD §5.2; R36 spike, see
 * NOTES.md). Next 16 sets `X-Forwarded-For` to the socket peer only when the request has none;
 * it never appends. So each of the `hops` trusted proxies appends the address it saw, and the
 * client is entry `n − hops`. With no proxy (`hops = 0`) the right-most entry is the peer that
 * Next filled in (or, if the client sent its own header, the client's claim: dev only).
 * Returns null when unknown; the left-most value is never trusted on its own.
 */
export function clientIp(h: Headers, hops: number): string | null {
  const xff = h.get("x-forwarded-for");
  const entries = xff ? xff.split(",").map((e) => e.trim()) : [];
  const idx = hops === 0 ? entries.length - 1 : entries.length - hops;
  const candidate = idx >= 0 ? entries[idx] : undefined;
  const ip = candidate ? normalise(candidate) : null;
  if (ip === null) noteUnknown(entries.length);
  return ip;
}

/** Strips an IPv4-mapped IPv6 prefix; rejects anything that isn't an IP address. */
function normalise(v: string): string | null {
  const s = v.startsWith("::ffff:") && isIP(v.slice(7)) === 4 ? v.slice(7) : v;
  return isIP(s) === 0 ? null : s;
}

let lastUnknownLog = 0;

function noteUnknown(entries: number): void {
  const now = Date.now();
  if (now - lastUnknownLog < 60_000) return;
  lastUnknownLog = now;
  logger().warn({ event: "security.client_ip_unknown", xff_entries: entries }, "client IP unknown");
}

/** Tests only. */
export function resetClientIpLogForTests(): void {
  lastUnknownLog = 0;
}
