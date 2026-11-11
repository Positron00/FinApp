import { randomBytes } from "node:crypto";
import { decodeJwt } from "jose";
import { http, HttpResponse } from "msw";

import { POST as accept, GET as acceptGet } from "@/app/api/auth/accept/route";
import { POST as resetConfirm } from "@/app/api/auth/reset/confirm/route";
import { POST as resetRequest } from "@/app/api/auth/reset/request/route";
import { metricsApi } from "@/lib/metrics";
import { resolveSession } from "@/lib/require-session";
import { sessKey, sessionKeyOf, userSessKey } from "@/lib/session";

import { COMPANY_ID, IDENTITY } from "../helpers/env";
import { errorBody, sessionUser } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn, USER_ID } from "../helpers/request";
import { t } from "../helpers/state";

setupMsw();

// Generated at runtime so no literal looks like a credential (testing.md §9).
const TOKEN = randomBytes(24).toString("base64url");
const PASSWORD = "a long enough password";

async function errorOf(res: Response) {
  return (
    (await res.json()) as {
      error: { code: string; message: string; details?: unknown; request_id: string };
    }
  ).error;
}

function post(path: string, body: unknown, extra: Parameters<typeof makeReq>[1] = {}) {
  return makeReq(path, { method: "POST", body, ...extra });
}

describe("POST /api/auth/accept", () => {
  function acceptOk() {
    server.use(http.post(`${IDENTITY}/v1/invites/accept`, () => HttpResponse.json(sessionUser)));
  }

  // PAY-103, PAY-106, C6: SessionUser → {user: Me}, a new session created via accept.
  it("creates a session and returns Me", async () => {
    acceptOk();
    const res = await accept(
      post("/api/auth/accept", { token: TOKEN, password: PASSWORD }),
      params(),
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { user: Record<string, unknown> };
    expect(body.user).toMatchObject({ id: USER_ID, status: "active" });
    expect(body.user).not.toHaveProperty("company_id");
    const cookie = res.cookies.get("sid")!;
    expect(cookie.value).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(cookie).toMatchObject({ httpOnly: true, secure: true, sameSite: "lax", path: "/" });
    const stored = await t.redis.hgetall(sessKey(sessionKeyOf(cookie.value)));
    expect(stored).toMatchObject({ user_id: USER_ID, cid: COMPANY_ID, name: "Olive Owner" });
    expect(t.logs.events("auth.session.created")[0]).toMatchObject({ via: "accept" });
    // PAY-NFR-07: the token never reaches a log line
    expect(t.logs.text()).not.toContain(TOKEN);
  });

  // PAY-600, R9, C16: service:web, aud=identity, no name/sid; X-Client-IP when known.
  it("calls identity with a service:web token and the client IP", async () => {
    acceptOk();
    await accept(
      post("/api/auth/accept", { token: TOKEN, password: PASSWORD }, { xff: "203.0.113.9" }),
      params(),
    );
    const call = seen[0]!;
    expect(call.headers.get("x-client-ip")).toBe("203.0.113.9");
    const claims = decodeJwt(call.headers.get("authorization")!.slice(7));
    expect(claims).toMatchObject({
      aud: "identity",
      sub: "service:web",
      roles: [],
      cid: COMPANY_ID,
    });
    expect(claims).not.toHaveProperty("name");
    expect(claims).not.toHaveProperty("sid");
    expect(await call.json()).toEqual({ token: TOKEN, password: PASSWORD });
  });

  // PAY-106: rotation: an old session cookie on the request is deleted.
  it("rotates an existing session", async () => {
    acceptOk();
    const old = await signIn();
    const res = await accept(
      post("/api/auth/accept", { token: TOKEN, password: PASSWORD }, { cookie: old }),
      params(),
    );
    const fresh = res.cookies.get("sid")!.value;
    expect(fresh).not.toBe(old);
    expect(await t.redis.exists(sessKey(sessionKeyOf(old)))).toBe(0);
  });

  // PAY-103: invite_invalid and password_too_weak keep status with catalogue messages.
  it.each([
    [
      "invite_invalid",
      400,
      "This invite link has expired or was already used. Ask your admin to send a new one.",
    ],
    ["password_too_weak", 422, "Use a password of at least 12 characters."],
    ["rate_limited", 429, "Too many requests. Try again in 30 seconds."],
  ])("maps %s", async (code, status, message) => {
    server.use(
      http.post(`${IDENTITY}/v1/invites/accept`, () =>
        HttpResponse.json(errorBody(code), { status, headers: { "Retry-After": "30" } }),
      ),
    );
    const res = await accept(
      post("/api/auth/accept", { token: TOKEN, password: PASSWORD }),
      params(),
    );
    expect(res.status).toBe(status);
    const e = await errorOf(res);
    expect(e).toMatchObject({ code, message });
    expect(JSON.stringify(e)).not.toContain("SELECT");
    expect(res.cookies.get("sid")).toBeUndefined();
  });

  // PAY-103, C14: a short password or a malformed token → 422 with no value echoed.
  it("validates the body without echoing values", async () => {
    const res = await accept(
      post("/api/auth/accept", { token: "short", password: "tiny-secret" }),
      params(),
    );
    expect(res.status).toBe(422);
    const e = await errorOf(res);
    expect(e.details).toEqual([
      { field: "body.token", issue: "invalid_format" },
      { field: "body.password", issue: "too_small" },
    ]);
    expect(JSON.stringify(e)).not.toContain("tiny-secret");
    expect(seen).toHaveLength(0);
  });

  // PAY-604: Origin is checked on public mutations.
  it("is 403 bad_origin from another origin", async () => {
    const res = await accept(
      post("/api/auth/accept", { token: TOKEN, password: PASSWORD }, { origin: "http://evil" }),
      params(),
    );
    expect(res.status).toBe(403);
    expect((await errorOf(res)).code).toBe("bad_origin");
  });

  // PAY-603: the limiter fails closed when Redis is down; identity isn't called.
  it("fails closed with 503 when Redis is down", async () => {
    t.redis.fail = true;
    const res = await accept(
      post("/api/auth/accept", { token: TOKEN, password: PASSWORD }),
      params(),
    );
    expect(res.status).toBe(503);
    expect(seen).toHaveLength(0);
    expect(t.logs.events("ratelimit.backend_error")[0]).toMatchObject({ mode: "fail_closed" });
  });

  // PAY-602: other methods → 405.
  it("is 405 for GET", async () => {
    const res = await acceptGet(makeReq("/api/auth/accept"), params());
    expect(res.status).toBe(405);
    expect(res.headers.get("allow")).toBe("POST");
  });
});

describe("POST /api/auth/reset/request", () => {
  // PAY-105, C68: always 202 {} (no account enumeration).
  it("answers 202 {}", async () => {
    server.use(
      http.post(
        `${IDENTITY}/v1/auth/password-reset/request`,
        () => new HttpResponse(null, { status: 202 }),
      ),
    );
    const res = await resetRequest(
      post("/api/auth/reset/request", { email: " Pat@Example.com" }, { xff: "198.51.100.4" }),
      params(),
    );
    expect(res.status).toBe(202);
    expect(await res.json()).toEqual({});
    expect(await seen[0]!.json()).toEqual({ email: "pat@example.com" });
    expect(seen[0]!.headers.get("x-client-ip")).toBe("198.51.100.4");
    expect(decodeJwt(seen[0]!.headers.get("authorization")!.slice(7)).sub).toBe("service:web");
  });

  // PAY-105, PAY-603: identity's 429 is forwarded with Retry-After.
  it("forwards 429 with Retry-After", async () => {
    server.use(
      http.post(`${IDENTITY}/v1/auth/password-reset/request`, () =>
        HttpResponse.json(errorBody("rate_limited"), {
          status: 429,
          headers: { "Retry-After": "12" },
        }),
      ),
    );
    const res = await resetRequest(post("/api/auth/reset/request", { email: "a@b.co" }), params());
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("12");
    expect((await errorOf(res)).message).toBe("Too many requests. Try again in 12 seconds.");
  });

  // PAY-105, C14: an invalid email → 422 with no value echoed.
  it("rejects an invalid email", async () => {
    const res = await resetRequest(
      post("/api/auth/reset/request", { email: "not-an-email" }),
      params(),
    );
    expect(res.status).toBe(422);
    const e = await errorOf(res);
    expect(e.details).toEqual([{ field: "body.email", issue: "invalid_format" }]);
    expect(JSON.stringify(e)).not.toContain("not-an-email");
  });

  // PAY-603: fails closed.
  it("fails closed with 503 when Redis is down", async () => {
    t.redis.fail = true;
    const res = await resetRequest(post("/api/auth/reset/request", { email: "a@b.co" }), params());
    expect(res.status).toBe(503);
    expect(seen).toHaveLength(0);
  });
});

describe("POST /api/auth/reset/confirm", () => {
  function confirmOk() {
    server.use(
      http.post(`${IDENTITY}/v1/auth/password-reset/confirm`, () =>
        HttpResponse.json({ user_id: USER_ID, roles_version: 2 }),
      ),
    );
  }

  // PAY-105, PAY-107, R19: every session of the user is deleted at once; 204; cookie cleared.
  it("revokes every session of the user and clears the cookie", async () => {
    confirmOk();
    const a = await signIn();
    const b = await signIn();
    const res = await resetConfirm(
      post("/api/auth/reset/confirm", { token: TOKEN, password: PASSWORD }, { cookie: a }),
      params(),
    );
    expect(res.status).toBe(204);
    const cleared = res.cookies.get("sid")!;
    expect(cleared.value).toBe("");
    expect(cleared.maxAge).toBe(0);
    expect(await t.redis.exists(sessKey(sessionKeyOf(a)))).toBe(0);
    expect(await t.redis.exists(sessKey(sessionKeyOf(b)))).toBe(0);
    expect(await t.redis.exists(userSessKey(USER_ID))).toBe(0);
    await expect(resolveSession(b)).rejects.toMatchObject({ status: 401 });
    expect(t.logs.events("auth.session.revoked")[0]).toMatchObject({
      reason: "password_reset",
      count: 2,
    });
    expect(t.logs.text()).not.toContain(TOKEN);
  });

  // PAY-105: reset_invalid and password_too_weak; sessions untouched.
  it.each([
    ["reset_invalid", 400, "This reset link has expired or was already used. Request a new one."],
    ["password_too_weak", 422, "Use a password of at least 12 characters."],
  ])("maps %s", async (code, status, message) => {
    server.use(
      http.post(`${IDENTITY}/v1/auth/password-reset/confirm`, () =>
        HttpResponse.json(errorBody(code), { status }),
      ),
    );
    const a = await signIn();
    const res = await resetConfirm(
      post("/api/auth/reset/confirm", { token: TOKEN, password: PASSWORD }),
      params(),
    );
    expect(res.status).toBe(status);
    expect(await errorOf(res)).toMatchObject({ code, message });
    expect(await t.redis.exists(sessKey(sessionKeyOf(a)))).toBe(1);
  });

  // PAY-107 §4.4: a Redis failure during the revoke still answers 204 and logs revoke_failed;
  // the current-session delete failing too is session.backend_error at error level and counts
  // in bff_session_backend_errors_total (§7.1, §7.2).
  it("answers 204 when the revoke fails", async () => {
    const counted = vi.spyOn(metricsApi, "sessionBackendError");
    server.use(
      http.post(`${IDENTITY}/v1/auth/password-reset/confirm`, () => {
        t.redis.fail = true; // identity has applied the reset; Redis fails afterwards
        return HttpResponse.json({ user_id: USER_ID, roles_version: 2 });
      }),
    );
    const res = await resetConfirm(
      post(
        "/api/auth/reset/confirm",
        { token: TOKEN, password: PASSWORD },
        { cookie: "x".repeat(43) },
      ),
      params(),
    );
    expect(res.status).toBe(204);
    expect(t.logs.events("auth.session.revoke_failed")[0]).toMatchObject({
      reason: "password_reset",
    });
    expect(t.logs.events("session.backend_error")[0]).toMatchObject({
      level: "error",
      op: "reset_confirm",
    });
    expect(counted).toHaveBeenCalledOnce();
    counted.mockRestore();
  });

  // PAY-603: fails closed.
  it("fails closed with 503 when Redis is down", async () => {
    t.redis.fail = true;
    const res = await resetConfirm(
      post("/api/auth/reset/confirm", { token: TOKEN, password: PASSWORD }),
      params(),
    );
    expect(res.status).toBe(503);
    expect(seen).toHaveLength(0);
  });

  // PAY-105: a non-JSON identity 200 body → 500, sessions kept.
  it("refuses an invalid identity response", async () => {
    server.use(
      http.post(`${IDENTITY}/v1/auth/password-reset/confirm`, () =>
        HttpResponse.json({ user_id: "nope" }),
      ),
    );
    const res = await resetConfirm(
      post("/api/auth/reset/confirm", { token: TOKEN, password: PASSWORD }),
      params(),
    );
    expect(res.status).toBe(500);
    expect(t.logs.events("bff.response_invalid")).toHaveLength(1);
  });
});
