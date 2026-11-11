import { GET as fallbackGet, POST as fallbackPost } from "@/app/api/[...path]/route";
import { DELETE as runDelete } from "@/app/api/pay-runs/[id]/route";
import { GET as livez } from "@/app/api/livez/route";
import { GET as listRuns } from "@/app/api/pay-runs/route";
import { GET as readyz } from "@/app/api/readyz/route";
import { POST as telemetry } from "@/app/api/telemetry/route";

import { applyEnv, TEST_ENV } from "../helpers/env";
import { RUN_ID } from "../helpers/fixtures";
import { makeReq, params, signIn } from "../helpers/request";
import { t } from "../helpers/state";

async function resetConfig() {
  const { resetConfigForTests } = await import("@/lib/config");
  resetConfigForTests();
}

describe("health routes", () => {
  // PAY-605, C4: liveness has no dependencies.
  it("livez is 200 with Redis down", async () => {
    t.redis.fail = true;
    const res = await livez(makeReq("/api/livez"), params());
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ status: "ok" });
  });

  // PAY-605, C4: readiness pings Redis.
  it("readyz is 200 with Redis up and 503 with Redis down", async () => {
    expect((await readyz(makeReq("/api/readyz"), params())).status).toBe(200);
    t.redis.fail = true;
    const res = await readyz(makeReq("/api/readyz"), params());
    expect(res.status).toBe(503);
    expect(((await res.json()) as { error: { code: string } }).error.code).toBe(
      "service_unavailable",
    );
  });

  // PAY-603: health routes aren't rate limited (no limiter keys written).
  it("health routes skip the limiter", async () => {
    await livez(makeReq("/api/livez"), params());
    expect([...t.redis.data.keys()].filter((k) => k.startsWith("web:rl:"))).toEqual([]);
  });
});

describe("fallbacks", () => {
  // PAY-602, C15: unknown /api/* → 404 not_found with the standard headers.
  it("unknown path → 404 not_found", async () => {
    for (const handler of [fallbackGet, fallbackPost]) {
      const res = await handler(
        handler === fallbackGet
          ? makeReq("/api/no-such-thing")
          : makeReq("/api/no-such-thing", { method: "POST", rawBody: "x" }),
        params({ path: ["no-such-thing"] } as unknown as Record<string, string>),
      );
      expect(res.status).toBe(404);
      expect(((await res.json()) as { error: { code: string } }).error.code).toBe("not_found");
      expect(res.headers.get("x-request-id")).toBeTruthy();
      expect(res.headers.get("cache-control")).toBe("no-store");
    }
  });

  // PAY-602, PAY-604: an unexported method → 405 with Allow and the standard headers.
  it("unexported method → 405 method_not_allowed", async () => {
    const res = await runDelete(
      makeReq(`/api/pay-runs/${RUN_ID}`, { method: "DELETE" }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(405);
    expect(res.headers.get("allow")).toBe("GET");
    expect(res.headers.get("x-trace-id")).toMatch(/^[0-9a-f]{32}$/);
    expect(res.headers.get("cache-control")).toBe("no-store");
  });
});

describe("standard headers", () => {
  // PAY-604: every BFF response, success and error (401 included), carries the headers.
  it("sets x-request-id, x-trace-id and Cache-Control on success and error", async () => {
    const ok = await livez(makeReq("/api/livez"), params());
    const unauth = await listRuns(makeReq("/api/pay-runs"), params());
    for (const res of [ok, unauth]) {
      expect(res.headers.get("x-request-id")).toMatch(/^[0-9a-f-]{36}$/);
      expect(res.headers.get("x-trace-id")).toMatch(/^[0-9a-f]{32}$/);
      expect(res.headers.get("cache-control")).toBe("no-store");
    }
    expect(unauth.status).toBe(401);
  });

  // PAY-604, NFR-09: a well-formed incoming x-request-id is kept and returned in the body.
  it("keeps a well-formed incoming request ID", async () => {
    const id = "0193a000-0000-7000-8000-00000000abcd";
    const res = await listRuns(
      makeReq("/api/pay-runs", { headers: { "x-request-id": id } }),
      params(),
    );
    expect(res.headers.get("x-request-id")).toBe(id);
    expect(((await res.json()) as { error: { request_id: string } }).error.request_id).toBe(id);
  });
});

describe("rate limits in the pipeline", () => {
  // PAY-603: the per-IP limit gives 429 rate_limited with Retry-After.
  it("limits per IP", async () => {
    applyEnv({ ...TEST_ENV, BFF_RATE_LIMIT_IP: "2" });
    await resetConfig();
    const req = () => listRuns(makeReq("/api/pay-runs", { xff: "198.51.100.4" }), params());
    expect((await req()).status).toBe(401);
    expect((await req()).status).toBe(401);
    const third = await req();
    expect(third.status).toBe(429);
    expect(Number(third.headers.get("retry-after"))).toBeGreaterThanOrEqual(1);
    expect(t.logs.events("ratelimit.limited")[0]).toMatchObject({ scope: "ip" });
  });

  // PAY-603: the per-session limit applies after the session check.
  it("limits per session", async () => {
    applyEnv({ ...TEST_ENV, BFF_RATE_LIMIT_SESSION: "1" });
    await resetConfig();
    const cookie = await signIn();
    const { GET: me } = await import("@/app/api/auth/me/route");
    // the first request passes the limiter (identity isn't mocked here, so stop at the handler)
    const first = await listRuns(makeReq("/api/pay-runs?limit=0", { cookie }), params());
    expect(first.status).toBe(422);
    const second = await me(makeReq("/api/auth/me", { cookie }), params());
    expect(second.status).toBe(429);
    expect(t.logs.events("ratelimit.limited")[0]).toMatchObject({ scope: "session" });
  });

  // PAY-603: outside login/accept/reset the IP limiter fails open, counted and logged.
  it("fails open when the limiter's Redis call fails", async () => {
    const original = t.redis.multi.bind(t.redis);
    t.redis.multi = () => {
      const m = original();
      m.exec = async () => {
        throw new Error("boom");
      };
      return m;
    };
    const res = await fallbackGet(
      makeReq("/api/x"),
      params({ path: ["x"] } as unknown as Record<string, string>),
    );
    expect(res.status).toBe(404);
    expect(t.logs.events("ratelimit.backend_error")[0]).toMatchObject({
      mode: "fail_open",
      scope: "ip",
    });
  });
});

describe("POST /api/telemetry", () => {
  // §7.5: a vital is logged and answered 204.
  it("logs web.vitals", async () => {
    const res = await telemetry(
      makeReq("/api/telemetry", {
        method: "POST",
        body: { type: "vital", name: "LCP", value: 1200, rating: "good", route: "/pay-runs/[id]" },
      }),
      params(),
    );
    expect(res.status).toBe(204);
    expect(t.logs.events("web.vitals")[0]).toMatchObject({
      vital: "LCP",
      value: 1200,
      page: "/pay-runs/[id]",
    });
  });

  // PAY-NFR-07: outside dev, a browser error message is logged as its length only.
  it("logs only the message length outside dev", async () => {
    applyEnv({
      ...TEST_ENV,
      ENV: "uat",
      PUBLIC_BASE_URL: "https://payrun.example.com",
      SESSION_COOKIE_NAME: "__Host-sid",
      INTERNAL_JWT_KID: "uat-web-1",
      TRUSTED_PROXY_HOPS: "1",
    });
    await resetConfig();
    const res = await telemetry(
      makeReq("/api/telemetry", {
        method: "POST",
        origin: "https://payrun.example.com",
        body: { type: "error", message: "secret user text", route: "/login" },
      }),
      params(),
    );
    expect(res.status).toBe(204);
    expect(t.logs.events("web.client_error")[0]).toMatchObject({ message_len: 16 });
    expect(t.logs.text()).not.toContain("secret user text");
  });

  // §7.5, PAY-604: the 8 KB limit.
  it("rejects a body over 8 KB with 413", async () => {
    const res = await telemetry(
      makeReq("/api/telemetry", {
        method: "POST",
        body: { type: "error", message: "x".repeat(9000), route: "/" },
      }),
      params(),
    );
    expect(res.status).toBe(413);
    expect(t.logs.events("security.payload_too_large")).toHaveLength(1);
  });

  // §7.5: its own per-IP limit.
  it("has its own rate limit", async () => {
    applyEnv({ ...TEST_ENV, BFF_TELEMETRY_RATE_LIMIT_IP: "1" });
    await resetConfig();
    const send = () =>
      telemetry(
        makeReq("/api/telemetry", {
          method: "POST",
          body: { type: "error", message: "m", route: "/" },
        }),
        params(),
      );
    expect((await send()).status).toBe(204);
    expect((await send()).status).toBe(429);
  });
});
