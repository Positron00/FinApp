import { http, HttpResponse } from "msw";

import { PAYROLL } from "../helpers/env";
import { payRun } from "../helpers/fixtures";
import { server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn } from "../helpers/request";
import type { t as State } from "../helpers/state";

/**
 * PAY-603 at the production values (web LLD §5.1, §9.1 `rate-limit.int.test.ts`): 120 requests
 * a minute per session and 300 per IP; the 121st and 301st are 429 with `Retry-After`. Two
 * "replicas" (fresh module graphs) share one store, so they share counts. The store here is the
 * in-memory fake behind the same `RedisLike` port; the Testcontainers variant needs the
 * `testcontainers` dev dependency, which isn't in the workspace yet.
 */
setupMsw();

const START = new Date("2026-11-11T10:00:05Z"); // early in a window: no carry-over from the last

/**
 * This test's fake store and log capture. Read before any `vi.resetModules()`: the shared setup
 * writes them into the module registry current at `beforeEach`.
 */
let t: typeof State;

async function freshRoutes() {
  vi.resetModules();
  const { setRedisForTests } = await import("@/lib/redis");
  setRedisForTests(t.redis); // the shared store
  (await import("@/lib/logger")).setLogSinkForTests(t.logs);
  return {
    runs: (await import("@/app/api/pay-runs/route")).GET,
    fallback: (await import("@/app/api/[...path]/route")).GET,
  };
}

describe("PAY-603 production limits", () => {
  beforeEach(async () => {
    t = { ...(await import("../helpers/state")).t };
    vi.useFakeTimers({ now: START, toFake: ["Date"] });
  });

  // PAY-603: 120/min per session, counted across two replicas; the 121st request is 429.
  it("limits a session at 120 per minute across replicas", async () => {
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs`, () =>
        HttpResponse.json({ items: [payRun()], next_cursor: null }),
      ),
    );
    const cookie = await signIn({ roles: ["approver"] });
    const a = await freshRoutes();
    const b = await freshRoutes();
    for (let i = 0; i < 120; i++) {
      const handler = i % 2 === 0 ? a.runs : b.runs; // alternate replicas
      // a distinct IP per request, so only the session budget is spent
      const res = await handler(
        makeReq("/api/pay-runs", { cookie, xff: `198.51.100.${(i % 200) + 1}` }),
        params(),
      );
      expect(res.status).toBe(200);
    }
    const denied = await b.runs(makeReq("/api/pay-runs", { cookie, xff: "203.0.113.1" }), params());
    expect(denied.status).toBe(429);
    expect(((await denied.json()) as { error: { code: string } }).error.code).toBe("rate_limited");
    const ra = Number(denied.headers.get("retry-after"));
    expect(ra).toBeGreaterThanOrEqual(1);
    expect(ra).toBeLessThanOrEqual(60);
    expect(t.logs.events("ratelimit.limited").at(-1)).toMatchObject({ scope: "session" });
    // PAY-NFR-07: no raw IP or session key in the limiter keys
    const keys = [...t.redis.data.keys()].filter((k) => k.startsWith("web:rl:"));
    expect(keys.some((k) => k.includes("198.51.100") || k.includes(cookie))).toBe(false);
  });

  // PAY-603: 300/min per IP, counted across two replicas; the 301st request is 429.
  it("limits an IP at 300 per minute across replicas", async () => {
    const a = await freshRoutes();
    const b = await freshRoutes();
    const req = () => makeReq("/api/no-such-thing", { xff: "198.51.100.77" });
    for (let i = 0; i < 300; i++) {
      const res = await (i % 2 === 0 ? a.fallback : b.fallback)(req(), params());
      expect(res.status).toBe(404);
    }
    const denied = await a.fallback(req(), params());
    expect(denied.status).toBe(429);
    expect(Number(denied.headers.get("retry-after"))).toBeGreaterThanOrEqual(1);
    // another IP still gets through
    expect((await a.fallback(makeReq("/api/x", { xff: "198.51.100.78" }), params())).status).toBe(
      404,
    );
  });
});
