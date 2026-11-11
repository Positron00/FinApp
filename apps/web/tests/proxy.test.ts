import { readFileSync } from "node:fs";
import path from "node:path";

import { NextRequest } from "next/server";

import { proxy } from "@/proxy";

import { applyEnv, ORIGIN, TEST_ENV } from "./helpers/env";

/** PAY-604, PAY-106, PAY-601: the interceptor checks cookie presence and sets the CSP nonce. */
const req = (p: string, headers: Record<string, string> = {}) =>
  new NextRequest(new URL(p, ORIGIN), { headers });

describe("proxy", () => {
  // PAY-106: open paths pass without a cookie.
  it.each([
    "/login",
    "/accept",
    "/reset",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/telemetry",
    "/api/livez",
    "/api/readyz",
    "/api/pay-runs",
    "/api/no-such-path",
  ])("lets %s through without a cookie", (p) => {
    const res = proxy(req(p));
    expect(res.status).toBe(200);
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });

  // PAY-604, C63: /api/* is never gated here, so the route handler answers 401 (or 404 for
  // an unknown path) with x-request-id, x-trace-id and Cache-Control; the proxy adds no body.
  it("never answers /api/* itself", () => {
    const res = proxy(req("/api/pay-runs"));
    expect(res.headers.get("x-middleware-next")).toBe("1");
    expect(res.headers.get("x-middleware-request-x-request-id")).toBe(
      res.headers.get("x-request-id"),
    );
  });

  // PAY-106: a page without the cookie → 307 /login?next=<path+search>.
  it("redirects pages to /login with next", () => {
    const res = proxy(req("/pay-runs/abc?tab=payouts"));
    expect(res.status).toBe(307);
    const loc = new URL(res.headers.get("location")!);
    expect(loc.pathname).toBe("/login");
    expect(loc.searchParams.get("next")).toBe("/pay-runs/abc?tab=payouts");
  });

  // ADR 0009, PAY-604: with a cookie, the request gets x-nonce, x-request-id and the CSP.
  it("passes with a cookie and sets the nonce on the request and the CSP on the response", () => {
    const res = proxy(req("/pay-runs", { cookie: "sid=anything" }));
    expect(res.headers.get("x-middleware-next")).toBe("1");
    const csp = res.headers.get("content-security-policy")!;
    const nonce = /'nonce-([^']+)'/.exec(csp)![1]!;
    expect(res.headers.get("x-middleware-request-x-nonce")).toBe(nonce);
    expect(res.headers.get("x-middleware-request-content-security-policy")).toBe(csp);
    expect(res.headers.get("x-middleware-request-x-request-id")).toBe(
      res.headers.get("x-request-id"),
    );
    const other = proxy(req("/pay-runs", { cookie: "sid=anything" }));
    expect(other.headers.get("content-security-policy")).not.toBe(csp);
  });

  // PAY-604, NFR-09: a well-formed incoming x-request-id is kept; a malformed one replaced.
  it("keeps a valid request ID and replaces an invalid one", () => {
    const id = "0193a000-0000-7000-8000-00000000abcd";
    expect(proxy(req("/login", { "x-request-id": id })).headers.get("x-request-id")).toBe(id);
    expect(
      proxy(req("/login", { "x-request-id": "<script>" })).headers.get("x-request-id"),
    ).toMatch(/^[0-9a-f-]{36}$/);
  });

  // PAY-604: HSTS in prod only.
  it("adds HSTS only in prod", () => {
    expect(proxy(req("/login")).headers.get("strict-transport-security")).toBeNull();
    applyEnv({ ...TEST_ENV, ENV: "prod", SESSION_COOKIE_NAME: "__Host-sid" });
    expect(proxy(req("/login")).headers.get("strict-transport-security")).toBe(
      "max-age=31536000; includeSubDomains",
    );
    expect(proxy(req("/x", { cookie: "__Host-sid=v" })).status).toBe(200);
  });

  // PAY-601: presence only, no Redis: proxy.ts imports neither the redis nor the session module.
  it("never touches Redis", () => {
    const src = readFileSync(path.join(__dirname, "../src/proxy.ts"), "utf8");
    expect(src).not.toMatch(/lib\/(redis|session|require-session|config)/);
  });
});
