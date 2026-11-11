import { NextRequest } from "next/server";
import { z } from "zod";

import { readJson } from "@/lib/body";
import { clientIp } from "@/lib/client-ip";
import { buildCsp, newNonce } from "@/lib/csp";
import { checkOrigin } from "@/lib/origin";
import { limit, limiterSubject, RateLimiterBackendError } from "@/lib/rate-limit";

import { ORIGIN } from "../helpers/env";
import { t } from "../helpers/state";

const xff = (v?: string) => new Headers(v === undefined ? {} : { "x-forwarded-for": v });

describe("clientIp (R36, §5.2)", () => {
  // PAY-603, R36: hops 0 takes the right-most entry (Next fills it from the socket when absent).
  it("hops 0: the right-most entry", () => {
    expect(clientIp(xff("127.0.0.1"), 0)).toBe("127.0.0.1");
    expect(clientIp(xff("198.51.100.1, 203.0.113.9"), 0)).toBe("203.0.113.9");
  });

  // PAY-603: hops 1 and 2 count the trusted proxies' appended entries from the right.
  it("hops 1..2: entry n − hops", () => {
    expect(clientIp(xff("203.0.113.9"), 1)).toBe("203.0.113.9");
    expect(clientIp(xff("6.6.6.6, 203.0.113.9"), 1)).toBe("203.0.113.9");
    expect(clientIp(xff("6.6.6.6, 203.0.113.9, 10.0.0.2"), 2)).toBe("203.0.113.9");
    expect(clientIp(xff("1.1.1.1, 6.6.6.6, 203.0.113.9, 10.0.0.2"), 2)).toBe("203.0.113.9");
  });

  // PAY-603: too few entries, a missing header or an invalid value → null (unknown).
  it("returns null when unknown", () => {
    expect(clientIp(xff(), 0)).toBeNull();
    expect(clientIp(xff("203.0.113.9"), 2)).toBeNull();
    expect(clientIp(xff("not-an-ip"), 0)).toBeNull();
    expect(t.logs.events("security.client_ip_unknown")).toHaveLength(1); // at most once a minute
  });

  // PAY-603: never the left-most value when it isn't the client's slot.
  it("never trusts the left-most value on its own", () => {
    expect(clientIp(xff("6.6.6.6, 203.0.113.9"), 1)).not.toBe("6.6.6.6");
  });

  // PAY-603: IPv6 and IPv4-mapped forms.
  it("accepts IPv6 and unwraps IPv4-mapped addresses", () => {
    expect(clientIp(xff("2001:db8::1"), 0)).toBe("2001:db8::1");
    expect(clientIp(xff("::ffff:203.0.113.9"), 0)).toBe("203.0.113.9");
  });
});

describe("limit (§5.1)", () => {
  // PAY-603: deny at limit + 1 with Retry-After; the sliding estimate spans the boundary.
  it("denies at limit + 1 and weighs the previous window", async () => {
    vi.useFakeTimers({ now: new Date("2026-11-10T00:00:00Z"), toFake: ["Date"] });
    const s = limiterSubject("203.0.113.9");
    for (let i = 0; i < 3; i++) expect(await limit("ip", s, 3)).toEqual({ ok: true });
    expect(await limit("ip", s, 3)).toEqual({ ok: false, retryAfterS: 60 });
    vi.setSystemTime(new Date("2026-11-10T00:01:30Z")); // half-way through the next window
    // estimate = 1 + 4 × 0.5 = 3 → allowed; the next is 2 + 2 = 4 → denied
    expect(await limit("ip", s, 3)).toEqual({ ok: true });
    expect(await limit("ip", s, 3)).toEqual({ ok: false, retryAfterS: 30 });
  });

  // PAY-603, PAY-NFR-08: subjects are hashed; no raw IP in keys; unknown shares one bucket.
  it("hashes subjects", async () => {
    await limit("ip", limiterSubject("203.0.113.9"), 10);
    const keys = [...t.redis.data.keys()];
    expect(keys.every((k) => !k.includes("203.0.113.9"))).toBe(true);
    expect(keys[0]).toMatch(/^web:rl:ip:[0-9a-f]{16}:\d+$/);
    expect(limiterSubject(null)).toBe(limiterSubject("unknown"));
    expect(t.redis.pttl(keys[0]!)).toBeLessThanOrEqual(120_000);
  });

  // PAY-603: a Redis error surfaces as RateLimiterBackendError (the route decides open/closed).
  it("throws RateLimiterBackendError on Redis errors", async () => {
    t.redis.fail = true;
    await expect(limit("session", "x", 1)).rejects.toBeInstanceOf(RateLimiterBackendError);
  });
});

describe("checkOrigin (§5.4)", () => {
  const req = (method: string, origin?: string) =>
    new Request(`${ORIGIN}/api/x`, { method, headers: origin ? { origin } : {} });

  // PAY-604: same origin passes; GET isn't checked.
  it("passes the same origin and GETs", () => {
    expect(() => checkOrigin(req("POST", ORIGIN), "/x")).not.toThrow();
    expect(() => checkOrigin(req("GET", "http://evil.example"), "/x")).not.toThrow();
  });

  // PAY-604: other scheme, host or port, null, or missing → 403 bad_origin.
  it.each([
    "https://127.0.0.1:4100",
    "http://localhost:4100",
    "http://127.0.0.1:4101",
    "null",
    undefined,
  ])("refuses %s", (origin) => {
    expect(() => checkOrigin(req("PATCH", origin), "/x")).toThrow(
      expect.objectContaining({ code: "bad_origin", status: 403 }),
    );
  });
});

describe("readJson (§5.3)", () => {
  const schema = z.strictObject({ a: z.number() });
  const make = (
    body: BodyInit | null,
    headers: Record<string, string> = { "content-type": "application/json" },
  ) =>
    new NextRequest(`${ORIGIN}/api/x`, {
      method: "POST",
      body,
      headers,
      duplex: "half",
    } as ConstructorParameters<typeof NextRequest>[1]);

  // PAY-604: Content-Length over the limit → 413 without reading.
  it("rejects a declared length over the limit", async () => {
    const r = make("{}", { "content-type": "application/json", "content-length": "70000" });
    await expect(readJson(r, schema, "/x", 65536)).rejects.toMatchObject({ status: 413 });
    expect(r.bodyUsed).toBe(false);
  });

  // PAY-604: a chunked body over the limit → 413.
  it("caps a streamed body", async () => {
    const stream = new ReadableStream({
      start(c) {
        for (let i = 0; i < 10; i++) c.enqueue(new Uint8Array(1000));
        c.close();
      },
    });
    await expect(readJson(make(stream), schema, "/x", 4096)).rejects.toMatchObject({
      status: 413,
      code: "payload_too_large",
    });
  });

  // PAY-602: bad JSON → 400 invalid_json; wrong content type → 422; schema errors → 422 with no values.
  it("maps bad JSON, content type and schema errors", async () => {
    await expect(readJson(make("{nope"), schema, "/x")).rejects.toMatchObject({
      status: 400,
      code: "invalid_json",
    });
    await expect(
      readJson(make('{"a":1}', { "content-type": "text/plain" }), schema, "/x"),
    ).rejects.toMatchObject({
      status: 422,
    });
    const err = await readJson(make('{"a":"secret","b":2}'), schema, "/x").catch((e: unknown) => e);
    expect(err).toMatchObject({ status: 422 });
    expect(JSON.stringify((err as { details: unknown }).details)).not.toContain("secret");
    await expect(
      readJson(
        make('{"a":1}', { "content-type": "application/json; charset=utf-8" }),
        schema,
        "/x",
      ),
    ).resolves.toEqual({ a: 1 });
  });
});

describe("CSP (§5.5, ADR 0009)", () => {
  // PAY-604: nonce and strict-dynamic; unsafe-eval and ws: only in dev; upgrade on https.
  it("builds the policy per env", () => {
    const dev = buildCsp("N", "dev", "http://127.0.0.1:4100");
    expect(dev).toContain("script-src 'self' 'nonce-N' 'strict-dynamic' 'unsafe-eval'");
    expect(dev).toContain("connect-src 'self' ws:");
    expect(dev).not.toContain("upgrade-insecure-requests");
    const prod = buildCsp("N", "prod", "https://payrun.example.com");
    expect(prod).not.toContain("unsafe-eval");
    expect(prod).not.toContain("ws:");
    expect(prod).toContain("frame-ancestors 'none'");
    expect(prod).toContain("object-src 'none'");
    expect(prod).toContain("upgrade-insecure-requests");
  });

  // PAY-604: a unique nonce per request.
  it("makes unique nonces", () => {
    const a = newNonce();
    expect(a).toMatch(/^[A-Za-z0-9+/]{22}==$/);
    expect(newNonce()).not.toBe(a);
  });
});
