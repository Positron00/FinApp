import { http, HttpResponse } from "msw";

import { requireSession, requireSessionForPage, resolveSession } from "@/lib/require-session";
import { sessKey, sessionKeyOf, userSessKey } from "@/lib/session";

import { IDENTITY } from "../helpers/env";
import { errorBody, meOut } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { signIn, USER_ID } from "../helpers/request";
import { t } from "../helpers/state";

const cookieJar = vi.hoisted(() => ({ value: undefined as string | undefined }));
vi.mock("next/headers", () => ({
  cookies: async () => ({ get: () => (cookieJar.value ? { value: cookieJar.value } : undefined) }),
}));
const redirectMock = vi.hoisted(() =>
  vi.fn((url: string) => {
    throw new Error(`REDIRECT ${url}`);
  }),
);
vi.mock("next/navigation", () => ({ redirect: redirectMock }));

setupMsw();

const MIN = 60_000;

function meReturns(body: object, status = 200) {
  server.use(http.get(`${IDENTITY}/v1/users/me`, () => HttpResponse.json(body, { status })));
}

beforeEach(() => {
  vi.useFakeTimers({ now: new Date("2026-11-10T00:00:00Z"), toFake: ["Date"] });
  cookieJar.value = undefined;
});

describe("requireSession", () => {
  // PAY-106: no cookie → 401.
  it("is 401 without a cookie", async () => {
    await expect(resolveSession(undefined)).rejects.toMatchObject({
      status: 401,
      code: "unauthenticated",
    });
  });

  // PAY-106: an unknown session → 401 and the cookie is cleared.
  it("is 401 with cookie clearing for an unknown session", async () => {
    await expect(resolveSession("unknown")).rejects.toMatchObject({
      status: 401,
      clearCookie: true,
    });
  });

  // PAY-106, PAY-603: a Redis error → 503, never allow.
  it("is 503 when Redis fails", async () => {
    const cookie = await signIn();
    t.redis.fail = true;
    await expect(resolveSession(cookie)).rejects.toMatchObject({ status: 503 });
  });

  // PAY-107: the re-check is skipped under 60 s and runs at ≥ 60 s.
  it("re-checks only when roles_checked_at is 60 s old", async () => {
    const cookie = await signIn();
    meReturns(meOut);
    vi.advanceTimersByTime(59_000);
    await resolveSession(cookie);
    expect(seen).toHaveLength(0);
    vi.advanceTimersByTime(1_000);
    const s = await resolveSession(cookie);
    expect(seen).toHaveLength(1);
    expect(s.me?.id).toBe(USER_ID);
    expect(s.roles_checked_at).toBe(Date.now());
  });

  // PAY-107, C10: same version → roles_checked_at, roles and name updated from identity.
  it("updates roles and name after a successful re-check", async () => {
    const cookie = await signIn();
    meReturns({ ...meOut, name: "New Name", roles: ["approver"] });
    vi.advanceTimersByTime(MIN);
    const s = await resolveSession(cookie);
    expect(s.roles).toEqual(["approver"]);
    const stored = await t.redis.hgetall(sessKey(sessionKeyOf(cookie)));
    expect(stored.name).toBe("New Name");
    expect(Number(stored.roles_checked_at)).toBe(Date.now());
  });

  // PAY-107: a roles_version mismatch revokes every session of the user → 401.
  it("revokes all sessions on a roles_version mismatch", async () => {
    const a = await signIn();
    const b = await signIn();
    meReturns({ ...meOut, roles_version: 2 });
    vi.advanceTimersByTime(MIN);
    await expect(resolveSession(a)).rejects.toMatchObject({ status: 401 });
    expect(await t.redis.exists(sessKey(sessionKeyOf(b)))).toBe(0);
    expect(await t.redis.exists(userSessKey(USER_ID))).toBe(0);
    expect(t.logs.events("auth.session.revoked")[0]).toMatchObject({
      reason: "roles_version_mismatch",
    });
  });

  // PAY-107: a non-active status ends the session.
  it("is 401 when the user is inactive", async () => {
    const cookie = await signIn();
    meReturns({ ...meOut, status: "inactive" });
    vi.advanceTimersByTime(MIN);
    await expect(resolveSession(cookie)).rejects.toMatchObject({ status: 401 });
    expect(t.logs.events("auth.session.revoked")[0]).toMatchObject({ reason: "inactive" });
  });

  // PAY-107: identity 404 (user gone) → this session deleted, 401.
  it("is 401 when identity answers 404", async () => {
    const cookie = await signIn();
    meReturns(errorBody("user_not_found"), 404);
    vi.advanceTimersByTime(MIN);
    await expect(resolveSession(cookie)).rejects.toMatchObject({ status: 401 });
    expect(await t.redis.exists(sessKey(sessionKeyOf(cookie)))).toBe(0);
  });

  // PAY-107: a different user or company in the answer → 401 and bff.response_invalid.
  it("is 401 when identity returns another user", async () => {
    const cookie = await signIn();
    meReturns({ ...meOut, id: "0193a000-0000-7000-8000-0000000000ff" });
    vi.advanceTimersByTime(MIN);
    await expect(resolveSession(cookie)).rejects.toMatchObject({ status: 401 });
    expect(t.logs.events("bff.response_invalid")).toHaveLength(1);
  });

  // PAY-107: an invalid /me body → 500.
  it("is 500 when identity's body is invalid", async () => {
    const cookie = await signIn();
    meReturns({ id: USER_ID });
    vi.advanceTimersByTime(MIN);
    await expect(resolveSession(cookie)).rejects.toMatchObject({ status: 500 });
  });

  // C37: identity down within SESSION_RECHECK_GRACE_S → allowed with cached roles, logged.
  it("fails open inside the grace window", async () => {
    const cookie = await signIn();
    meReturns(errorBody("db_unavailable"), 503);
    vi.advanceTimersByTime(2 * MIN);
    const s = await resolveSession(cookie, { roles: ["approver"] });
    expect(s.roles).toContain("approver");
    expect(s.me).toBeUndefined();
    expect(t.logs.events("auth.session.recheck_deferred")[0]).toMatchObject({ age_s: 120 });
    const stored = await t.redis.hgetall(sessKey(sessionKeyOf(cookie)));
    expect(Number(stored.roles_checked_at)).toBe(Date.now() - 2 * MIN); // not updated
  });

  // C37, PAY-107: past the grace (300 s since the last check) → 503.
  it("is 503 past the grace window", async () => {
    const cookie = await signIn();
    meReturns(errorBody("db_unavailable"), 503);
    vi.advanceTimersByTime(300_000);
    await expect(resolveSession(cookie)).rejects.toMatchObject({
      status: 503,
      code: "service_unavailable",
    });
  });

  // PAY-107: the re-check is single-flighted per session within the process.
  it("shares one /me call between parallel requests", async () => {
    const cookie = await signIn();
    meReturns(meOut);
    vi.advanceTimersByTime(MIN);
    await Promise.all(Array.from({ length: 5 }, () => resolveSession(cookie)));
    expect(seen.filter((r) => r.url.endsWith("/v1/users/me"))).toHaveLength(1);
  });

  // PAY-601: a role mismatch → 403 with the session kept.
  it("is 403 forbidden_role without the role, and keeps the session", async () => {
    const cookie = await signIn({ roles: ["payroll_admin"] });
    await expect(resolveSession(cookie, { roles: ["approver"] })).rejects.toMatchObject({
      status: 403,
      code: "forbidden_role",
    });
    expect(await t.redis.exists(sessKey(sessionKeyOf(cookie)))).toBe(1);
  });

  // PAY-601: requireSession reads the cookie with cookies().
  it("reads the cookie from next/headers", async () => {
    cookieJar.value = await signIn();
    await expect(requireSession(["admin"])).resolves.toMatchObject({ user_id: USER_ID });
  });
});

describe("requireSessionForPage", () => {
  // PAY-106: pages redirect to /login?next= on 401.
  it("redirects to /login with next on 401", async () => {
    await expect(requireSessionForPage("/pay-runs?x=1")).rejects.toThrow(
      "REDIRECT /login?next=%2Fpay-runs%3Fx%3D1",
    );
  });

  // PAY-601: 403 → "forbidden" (the page renders <Forbidden/>).
  it("returns forbidden on 403", async () => {
    cookieJar.value = await signIn({ roles: ["approver"] });
    await expect(requireSessionForPage("/pay-runs/new", ["payroll_admin"])).resolves.toBe(
      "forbidden",
    );
  });

  // PAY-106: 503 is thrown for error.tsx.
  it("throws on 503", async () => {
    cookieJar.value = await signIn();
    t.redis.fail = true;
    await expect(requireSessionForPage("/dashboard")).rejects.toMatchObject({ status: 503 });
  });
});
