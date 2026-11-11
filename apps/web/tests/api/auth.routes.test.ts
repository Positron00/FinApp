import { decodeJwt, decodeProtectedHeader } from "jose";
import { http, HttpResponse } from "msw";

import { POST as login, GET as loginGet } from "@/app/api/auth/login/route";
import { POST as logout } from "@/app/api/auth/logout/route";
import { GET as me } from "@/app/api/auth/me/route";
import { metricsApi } from "@/lib/metrics";
import { sessKey, sessionKeyOf, userSessKey } from "@/lib/session";

import { COMPANY_ID, IDENTITY } from "../helpers/env";
import { errorBody, meOut, sessionUser } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn, USER_ID } from "../helpers/request";
import { t } from "../helpers/state";

setupMsw();

const CREDS = { email: "Owner@PayRun.example.com ", password: "correct horse battery" };

function loginOk() {
  server.use(http.post(`${IDENTITY}/v1/auth/login`, () => HttpResponse.json(sessionUser)));
}

describe("POST /api/auth/login", () => {
  // PAY-104, PAY-106: a SessionUser becomes {user: Me}, a session and a cookie.
  it("creates a session and returns Me", async () => {
    loginOk();
    const res = await login(makeReq("/api/auth/login", { method: "POST", body: CREDS }), params());
    expect(res.status).toBe(200);
    const body = (await res.json()) as { user: Record<string, unknown> };
    expect(body.user).toEqual({
      id: USER_ID,
      email: "owner@payrun.example.com",
      name: "Olive Owner",
      roles: ["admin", "payroll_admin", "approver"],
      roles_version: 1,
      status: "active",
    });
    expect(body.user).not.toHaveProperty("company_id"); // C6
    const cookie = res.cookies.get("sid");
    expect(cookie?.value).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(cookie).toMatchObject({ httpOnly: true, secure: true, sameSite: "lax", path: "/" });
    expect(cookie?.maxAge).toBe(604800);
    const k = sessionKeyOf(cookie!.value);
    const stored = await t.redis.hgetall(sessKey(k));
    expect(stored).toMatchObject({ user_id: USER_ID, cid: COMPANY_ID, name: "Olive Owner" });
    expect(await t.redis.smembers(userSessKey(USER_ID))).toEqual([k]);
  });

  // PAY-600, R9, C16: service:web token with aud=identity, no name/sid; X-Client-IP when known.
  it("calls identity with a service:web token and the client IP", async () => {
    loginOk();
    await login(
      makeReq("/api/auth/login", { method: "POST", body: CREDS, xff: "203.0.113.7" }),
      params(),
    );
    const call = seen.find((r) => r.url.endsWith("/v1/auth/login"))!;
    expect(call.headers.get("x-client-ip")).toBe("203.0.113.7");
    expect(call.headers.get("x-request-id")).toMatch(/^[0-9a-f-]{36}$/);
    const token = call.headers.get("authorization")!.replace("Bearer ", "");
    const claims = decodeJwt(token);
    expect(decodeProtectedHeader(token)).toMatchObject({ alg: "EdDSA", kid: "test-web-1" });
    expect(claims).toMatchObject({
      iss: "web",
      aud: "identity",
      sub: "service:web",
      roles: [],
      cid: COMPANY_ID,
    });
    expect(claims).not.toHaveProperty("name");
    expect(claims).not.toHaveProperty("sid");
    expect(await call.json()).toEqual({
      email: "owner@payrun.example.com",
      password: CREDS.password,
    });
  });

  // C16: X-Client-IP is omitted when the IP is unknown.
  it("omits X-Client-IP when the client IP is unknown", async () => {
    loginOk();
    await login(makeReq("/api/auth/login", { method: "POST", body: CREDS }), params());
    const call = seen.find((r) => r.url.endsWith("/v1/auth/login"))!;
    expect(call.headers.has("x-client-ip")).toBe(false);
  });

  // PAY-106: rotation, the session the old cookie pointed at is deleted.
  it("rotates: deletes the previous session and issues a new ID", async () => {
    loginOk();
    const old = await signIn();
    const res = await login(
      makeReq("/api/auth/login", { method: "POST", body: CREDS, cookie: old }),
      params(),
    );
    const fresh = res.cookies.get("sid")!.value;
    expect(fresh).not.toBe(old);
    expect(await t.redis.exists(sessKey(sessionKeyOf(old)))).toBe(0);
    expect(await t.redis.smembers(userSessKey(USER_ID))).toEqual([sessionKeyOf(fresh)]);
  });

  // PAY-104: invalid_credentials keeps 401 with the catalogue message; no cookie.
  it("maps 401 invalid_credentials", async () => {
    server.use(
      http.post(`${IDENTITY}/v1/auth/login`, () =>
        HttpResponse.json(errorBody("invalid_credentials"), { status: 401 }),
      ),
    );
    const res = await login(makeReq("/api/auth/login", { method: "POST", body: CREDS }), params());
    expect(res.status).toBe(401);
    const body = (await res.json()) as { error: Record<string, string> };
    expect(body.error.code).toBe("invalid_credentials");
    expect(body.error.message).toBe("Email or password is incorrect.");
    expect(JSON.stringify(body)).not.toContain("SELECT");
    expect(res.cookies.get("sid")).toBeUndefined();
  });

  // PAY-104, PAY-603: identity's lockout 429 is forwarded with Retry-After.
  it("forwards 429 rate_limited with Retry-After", async () => {
    server.use(
      http.post(`${IDENTITY}/v1/auth/login`, () =>
        HttpResponse.json(errorBody("rate_limited"), {
          status: 429,
          headers: { "Retry-After": "120" },
        }),
      ),
    );
    const res = await login(makeReq("/api/auth/login", { method: "POST", body: CREDS }), params());
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("120");
    expect(((await res.json()) as { error: { message: string } }).error.message).toBe(
      "Too many requests. Try again in 120 seconds.",
    );
  });

  // PAY-603: the login limiter fails closed when Redis is down; identity isn't called.
  it("fails closed with 503 when Redis is down", async () => {
    t.redis.fail = true;
    const res = await login(makeReq("/api/auth/login", { method: "POST", body: CREDS }), params());
    expect(res.status).toBe(503);
    expect(seen).toHaveLength(0);
    expect(t.logs.events("ratelimit.backend_error")[0]).toMatchObject({ mode: "fail_closed" });
  });

  // PAY-604: a cross-origin login is refused before identity is called.
  it("refuses a bad Origin", async () => {
    const res = await login(
      makeReq("/api/auth/login", { method: "POST", body: CREDS, origin: "http://localhost:4100" }),
      params(),
    );
    expect(res.status).toBe(403);
    expect(((await res.json()) as { error: { code: string } }).error.code).toBe("bad_origin");
    expect(seen).toHaveLength(0);
  });

  // PAY-602, C14: validation details carry the field and issue, never the value.
  it("rejects a malformed body with 422 and no echoed values", async () => {
    const res = await login(
      makeReq("/api/auth/login", {
        method: "POST",
        body: { email: "not-an-email", password: "x", extra: "secret-value" },
      }),
      params(),
    );
    expect(res.status).toBe(422);
    const text = await res.text();
    expect(text).not.toContain("not-an-email");
    expect(text).not.toContain("secret-value");
    const body = JSON.parse(text) as { error: { details: { field: string; issue: string }[] } };
    expect(body.error.details.map((d) => d.field)).toEqual(
      expect.arrayContaining(["body.email", "body"]),
    );
  });

  // PAY-104, PAY-NFR-08: an identity body that isn't a SessionUser → 500, no session.
  it("returns 500 and creates no session when identity's body is invalid", async () => {
    server.use(
      http.post(`${IDENTITY}/v1/auth/login`, () => HttpResponse.json({ user_id: "nope" })),
    );
    const res = await login(makeReq("/api/auth/login", { method: "POST", body: CREDS }), params());
    expect(res.status).toBe(500);
    expect(t.logs.events("bff.response_invalid")).toHaveLength(1);
    expect(t.redis.data.size).toBeGreaterThan(0); // limiter counters only
    expect([...t.redis.data.keys()].some((k) => k.startsWith("web:sess:"))).toBe(false);
  });

  // PAY-NFR-07: neither the password nor the token nor the cookie reaches the logs.
  it("never logs the password, the JWT or the cookie", async () => {
    loginOk();
    const res = await login(makeReq("/api/auth/login", { method: "POST", body: CREDS }), params());
    const token = seen[0]!.headers.get("authorization")!.slice(7);
    const text = t.logs.text();
    expect(text).not.toContain(CREDS.password);
    expect(text).not.toContain(token);
    expect(text).not.toContain(res.cookies.get("sid")!.value);
    expect(t.logs.events("http.request")[0]).toMatchObject({ status: 200, method: "POST" });
  });

  // PAY-602: GET on the login route is 405 with Allow.
  it("answers 405 for an unexported method", async () => {
    const res = await loginGet(makeReq("/api/auth/login"), params());
    expect(res.status).toBe(405);
    expect(res.headers.get("allow")).toBe("POST");
  });
});

describe("POST /api/auth/logout", () => {
  // PAY-106: sign-out deletes the session and its index entry, and clears the cookie.
  it("deletes the session and clears the cookie", async () => {
    const cookie = await signIn();
    const res = await logout(makeReq("/api/auth/logout", { method: "POST", cookie }), params());
    expect(res.status).toBe(204);
    expect(res.cookies.get("sid")).toMatchObject({ value: "", maxAge: 0 });
    expect(await t.redis.exists(sessKey(sessionKeyOf(cookie)))).toBe(0);
    expect(await t.redis.smembers(userSessKey(USER_ID))).toEqual([]);
  });

  // PAY-106: idempotent without a session.
  it("is a 204 without a session", async () => {
    const res = await logout(makeReq("/api/auth/logout", { method: "POST" }), params());
    expect(res.status).toBe(204);
  });

  // PAY-106 §4.5: best effort when Redis is down; logged at error and counted (§7.1, §7.2).
  it("still clears the cookie when Redis fails", async () => {
    const cookie = await signIn();
    const counted = vi.spyOn(metricsApi, "sessionBackendError");
    t.redis.fail = true;
    const res = await logout(makeReq("/api/auth/logout", { method: "POST", cookie }), params());
    expect(res.status).toBe(204);
    expect(res.cookies.get("sid")?.maxAge).toBe(0);
    const errors = t.logs.events("session.backend_error");
    expect(errors.find((e) => e.op === "logout")).toMatchObject({ level: "error" });
    // one count per logged backend error (the optional session load fails first)
    expect(counted).toHaveBeenCalledTimes(errors.length);
    counted.mockRestore();
  });
});

describe("GET /api/auth/me", () => {
  // PAY-106, PAY-107: a forced re-check; returns Me and refreshes roles and name (C10).
  it("re-checks with identity and returns Me", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${IDENTITY}/v1/users/me`, () =>
        HttpResponse.json({ ...meOut, name: "Olive O.", roles: ["admin", "approver"] }),
      ),
    );
    const res = await me(makeReq("/api/auth/me", { cookie }), params());
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      id: USER_ID,
      email: "owner@payrun.example.com",
      name: "Olive O.",
      roles: ["admin", "approver"],
      roles_version: 1,
      status: "active",
    });
    const call = seen.find((r) => r.url.endsWith("/v1/users/me"))!;
    const claims = decodeJwt(call.headers.get("authorization")!.slice(7));
    expect(claims).toMatchObject({
      aud: "identity",
      sub: USER_ID,
      name: "Olive Owner",
      cid: COMPANY_ID,
    });
    expect(claims.sid).toBe(sessionKeyOf(cookie).slice(0, 32));
    const stored = await t.redis.hgetall(sessKey(sessionKeyOf(cookie)));
    expect(stored.name).toBe("Olive O.");
    expect(JSON.parse(stored.roles!)).toEqual(["admin", "approver"]);
  });

  // PAY-106: no cookie → 401.
  it("is 401 without a session", async () => {
    const res = await me(makeReq("/api/auth/me"), params());
    expect(res.status).toBe(401);
  });

  // PAY-107: identity's 404 user_not_found ends the session with 401.
  it("ends the session when identity says the user is gone", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${IDENTITY}/v1/users/me`, () =>
        HttpResponse.json(errorBody("user_not_found"), { status: 404 }),
      ),
    );
    const res = await me(makeReq("/api/auth/me", { cookie }), params());
    expect(res.status).toBe(401);
    expect(res.cookies.get("sid")?.maxAge).toBe(0);
    expect(await t.redis.exists(sessKey(sessionKeyOf(cookie)))).toBe(0);
  });

  // C37: identity down inside the grace window → deferred; /me has no profile to return → 503.
  it("answers 503 when identity is unavailable", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${IDENTITY}/v1/users/me`, () =>
        HttpResponse.json(errorBody("db_unavailable"), { status: 503 }),
      ),
    );
    const res = await me(makeReq("/api/auth/me", { cookie }), params());
    expect(res.status).toBe(503);
  });
});
