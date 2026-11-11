import { decodeJwt } from "jose";
import { http, HttpResponse } from "msw";

import { POST as resend } from "@/app/api/invites/[id]/resend/route";
import { PATCH as patchUser, GET as getUserMethod } from "@/app/api/users/[id]/route";
import { POST as invite } from "@/app/api/users/invite/route";
import { GET as listUsers } from "@/app/api/users/route";
import { sessKey, sessionKeyOf, userSessKey } from "@/lib/session";

import { IDENTITY } from "../helpers/env";
import { errorBody, INVITE_ID, pendingInvite, TARGET_USER_ID, user } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn, USER_ID } from "../helpers/request";
import { t } from "../helpers/state";

setupMsw();

async function errorOf(res: Response) {
  return ((await res.json()) as { error: { code: string; message: string; details?: unknown } })
    .error;
}

const INVITE = { email: "Pat@PayRun.example.com", name: " Pat ", roles: ["payroll_admin"] };

describe("GET /api/users", () => {
  // PAY-108, C8, C11: admin; cursor/limit pass through; pending_invite_id kept.
  it("lists users with pass-through and a user token for identity", async () => {
    const cookie = await signIn({ roles: ["admin"] });
    server.use(
      http.get(`${IDENTITY}/v1/users`, () =>
        HttpResponse.json({ items: [user()], next_cursor: "n1" }),
      ),
    );
    const res = await listUsers(makeReq("/api/users?cursor=c0&limit=25", { cookie }), params());
    expect(res.status).toBe(200);
    const body = (await res.json()) as { items: { pending_invite_id: string }[] };
    expect(body.items[0]!.pending_invite_id).toBe(INVITE_ID);
    expect(Object.fromEntries(new URL(seen[0]!.url).searchParams)).toEqual({
      cursor: "c0",
      limit: "25",
    });
    const claims = decodeJwt(seen[0]!.headers.get("authorization")!.slice(7));
    expect(claims).toMatchObject({ aud: "identity", sub: USER_ID, name: "Olive Owner" });
  });

  // PAY-601, PAY-108: admin only.
  it("is 403 for a non-admin and calls nothing", async () => {
    const cookie = await signIn({ roles: ["payroll_admin", "approver"] });
    const res = await listUsers(makeReq("/api/users", { cookie }), params());
    expect(res.status).toBe(403);
    expect((await errorOf(res)).code).toBe("forbidden_role");
    expect(seen).toHaveLength(0);
  });

  // PAY-106: no session → 401.
  it("is 401 without a session", async () => {
    const res = await listUsers(makeReq("/api/users"), params());
    expect(res.status).toBe(401);
  });

  // PAY-602, C8: a bad cursor → 422.
  it("rejects a malformed cursor", async () => {
    const cookie = await signIn();
    const res = await listUsers(makeReq("/api/users?cursor=a%20b", { cookie }), params());
    expect(res.status).toBe(422);
    expect((await errorOf(res)).details).toEqual([
      { field: "query.cursor", issue: "invalid_format" },
    ]);
  });
});

describe("POST /api/users/invite", () => {
  // PAY-101: 201 pending user with pending_invite_id; body normalised.
  it("invites a user", async () => {
    const cookie = await signIn();
    server.use(
      http.post(`${IDENTITY}/v1/users/invite`, () => HttpResponse.json(user(), { status: 201 })),
    );
    const res = await invite(
      makeReq("/api/users/invite", { method: "POST", cookie, body: INVITE }),
      params(),
    );
    expect(res.status).toBe(201);
    expect(((await res.json()) as { status: string }).status).toBe("pending");
    expect(await seen[0]!.json()).toEqual({
      email: "pat@payrun.example.com",
      name: "Pat",
      roles: ["payroll_admin"],
    });
  });

  // PAY-101: user_email_taken.
  it("maps 409 user_email_taken", async () => {
    const cookie = await signIn();
    server.use(
      http.post(`${IDENTITY}/v1/users/invite`, () =>
        HttpResponse.json(errorBody("user_email_taken"), { status: 409 }),
      ),
    );
    const res = await invite(
      makeReq("/api/users/invite", { method: "POST", cookie, body: INVITE }),
      params(),
    );
    expect(res.status).toBe(409);
    expect(await errorOf(res)).toMatchObject({
      code: "user_email_taken",
      message: "A user with this email already exists.",
    });
  });

  // PAY-101, C14: duplicate, unknown or empty roles → 422 before identity.
  it.each([
    [["approver", "approver"], "custom"],
    [["owner"], "invalid_value"],
    [[], "too_small"],
  ])("rejects roles %j", async (roles, issue) => {
    const cookie = await signIn();
    const res = await invite(
      makeReq("/api/users/invite", { method: "POST", cookie, body: { ...INVITE, roles } }),
      params(),
    );
    expect(res.status).toBe(422);
    const details = (await errorOf(res)).details as { field: string; issue: string }[];
    expect(details[0]!.field.startsWith("body.roles")).toBe(true);
    expect(details[0]!.issue).toBe(issue);
    expect(seen).toHaveLength(0);
  });

  // PAY-604: Origin on mutations.
  it("is 403 bad_origin without an Origin", async () => {
    const cookie = await signIn();
    const res = await invite(
      makeReq("/api/users/invite", { method: "POST", cookie, body: INVITE, origin: null }),
      params(),
    );
    expect(res.status).toBe(403);
    expect((await errorOf(res)).code).toBe("bad_origin");
  });

  // PAY-601: admin only.
  it("is 403 for a payroll_admin", async () => {
    const cookie = await signIn({ roles: ["payroll_admin"] });
    const res = await invite(
      makeReq("/api/users/invite", { method: "POST", cookie, body: INVITE }),
      params(),
    );
    expect(res.status).toBe(403);
    expect(seen).toHaveLength(0);
  });
});

describe("PATCH /api/users/[id]", () => {
  function patchOk(over: Record<string, unknown> = {}) {
    server.use(
      http.patch(`${IDENTITY}/v1/users/${TARGET_USER_ID}`, () =>
        HttpResponse.json(user({ status: "active", pending_invite_id: null, ...over })),
      ),
    );
  }
  const patch = (cookie: string, body: unknown, id = TARGET_USER_ID) =>
    patchUser(makeReq(`/api/users/${id}`, { method: "PATCH", cookie, body }), params({ id }));

  // PAY-107, PAY-108: a roles change revokes every session of the target at once.
  it("revokes the target's sessions on a roles change", async () => {
    const cookie = await signIn();
    const target1 = await signIn({ user_id: TARGET_USER_ID, roles: ["approver"] });
    const target2 = await signIn({ user_id: TARGET_USER_ID, roles: ["approver"] });
    patchOk({ roles: ["payroll_admin"] });
    const res = await patch(cookie, { roles: ["payroll_admin"] });
    expect(res.status).toBe(200);
    expect(await seen[0]!.json()).toEqual({ roles: ["payroll_admin"] });
    expect(await t.redis.exists(sessKey(sessionKeyOf(target1)))).toBe(0);
    expect(await t.redis.exists(sessKey(sessionKeyOf(target2)))).toBe(0);
    expect(await t.redis.exists(userSessKey(TARGET_USER_ID))).toBe(0);
    // the admin's own session is untouched
    expect(await t.redis.exists(sessKey(sessionKeyOf(cookie)))).toBe(1);
    expect(t.logs.events("auth.session.revoked")[0]).toMatchObject({
      reason: "role_change",
      count: 2,
    });
  });

  // PAY-107, PAY-108: deactivation revokes with reason "deactivated".
  it("revokes with reason deactivated on status inactive", async () => {
    const cookie = await signIn();
    await signIn({ user_id: TARGET_USER_ID });
    patchOk({ status: "inactive" });
    const res = await patch(cookie, { status: "inactive" });
    expect(res.status).toBe(200);
    expect(t.logs.events("auth.session.revoked")[0]).toMatchObject({ reason: "deactivated" });
  });

  // R37, C25: no user reactivation: status "active" → 422; an empty body → 422.
  it.each([
    [{ status: "active" }, "body.status"],
    [{}, "body"],
    [{ roles: ["admin"], extra: 1 }, "body"],
  ])("rejects %j", async (body, field) => {
    const cookie = await signIn();
    const res = await patch(cookie, body);
    expect(res.status).toBe(422);
    const details = (await errorOf(res)).details as { field: string }[];
    expect(details[0]!.field).toBe(field);
    expect(seen).toHaveLength(0);
  });

  // PAY-108: identity's rule errors.
  it.each([
    ["cannot_modify_self", 409, "You can't change your own roles or status. Ask another admin."],
    ["last_admin", 409, "The company must keep at least one active admin."],
    ["user_not_found", 404, "That user wasn't found."],
  ])("maps %s and revokes nothing", async (code, status, message) => {
    const cookie = await signIn();
    const target = await signIn({ user_id: TARGET_USER_ID });
    server.use(
      http.patch(`${IDENTITY}/v1/users/${TARGET_USER_ID}`, () =>
        HttpResponse.json(errorBody(code), { status }),
      ),
    );
    const res = await patch(cookie, { roles: ["approver"] });
    expect(res.status).toBe(status);
    expect(await errorOf(res)).toMatchObject({ code, message });
    expect(await t.redis.exists(sessKey(sessionKeyOf(target)))).toBe(1);
  });

  // PAY-107 §4.4: a Redis failure during the revoke still returns identity's 200.
  it("returns 200 and logs revoke_failed when Redis fails during the revoke", async () => {
    const cookie = await signIn();
    server.use(
      http.patch(`${IDENTITY}/v1/users/${TARGET_USER_ID}`, () => {
        t.redis.fail = true;
        return HttpResponse.json(user({ status: "active", pending_invite_id: null }));
      }),
    );
    const res = await patch(cookie, { roles: ["approver"] });
    expect(res.status).toBe(200);
    expect(t.logs.events("auth.session.revoke_failed")[0]).toMatchObject({
      reason: "role_change",
    });
  });

  // PAY-602: a malformed [id] → 422 before identity.
  it("rejects a malformed id", async () => {
    const cookie = await signIn();
    const res = await patch(cookie, { roles: ["approver"] }, "not-a-uuid");
    expect(res.status).toBe(422);
    expect((await errorOf(res)).details).toEqual([{ field: "path.id", issue: "invalid_format" }]);
  });

  // PAY-601, PAY-604: role and Origin.
  it("is 403 for a non-admin and for a bad Origin", async () => {
    const pa = await signIn({ roles: ["payroll_admin"] });
    expect((await patch(pa, { roles: ["approver"] })).status).toBe(403);
    const admin = await signIn();
    const res = await patchUser(
      makeReq(`/api/users/${TARGET_USER_ID}`, {
        method: "PATCH",
        cookie: admin,
        body: { roles: ["approver"] },
        origin: "http://localhost:4100",
      }),
      params({ id: TARGET_USER_ID }),
    );
    expect((await errorOf(res)).code).toBe("bad_origin");
    expect(seen).toHaveLength(0);
  });

  // PAY-602: GET isn't supported here.
  it("is 405 for GET", async () => {
    const res = await getUserMethod(makeReq(`/api/users/${TARGET_USER_ID}`), params());
    expect(res.status).toBe(405);
    expect(res.headers.get("allow")).toBe("PATCH");
  });
});

describe("POST /api/invites/[id]/resend", () => {
  const call = (cookie: string, id = INVITE_ID) =>
    resend(makeReq(`/api/invites/${id}/resend`, { method: "POST", cookie }), params({ id }));

  // PAY-102, R17: 200 PendingInvite (expires_at null until sent).
  it("resends an invite", async () => {
    const cookie = await signIn();
    server.use(
      http.post(`${IDENTITY}/v1/invites/${INVITE_ID}/resend`, () =>
        HttpResponse.json(pendingInvite()),
      ),
    );
    const res = await call(cookie);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(pendingInvite());
  });

  // PAY-102: invite_not_pending, invite_not_found.
  it.each([
    ["invite_not_pending", 409, "This user has already accepted their invite."],
    ["invite_not_found", 404, "That invite wasn't found."],
  ])("maps %s", async (code, status, message) => {
    const cookie = await signIn();
    server.use(
      http.post(`${IDENTITY}/v1/invites/${INVITE_ID}/resend`, () =>
        HttpResponse.json(errorBody(code), { status }),
      ),
    );
    const res = await call(cookie);
    expect(res.status).toBe(status);
    expect(await errorOf(res)).toMatchObject({ code, message });
  });

  // PAY-601, PAY-602: role and malformed id.
  it("is 403 for non-admins and 422 for a bad id", async () => {
    const approver = await signIn({ roles: ["approver"] });
    expect((await call(approver)).status).toBe(403);
    const admin = await signIn();
    expect((await call(admin, "x")).status).toBe(422);
    expect(seen).toHaveLength(0);
  });

  // PAY-106: 401 without a session.
  it("is 401 without a session", async () => {
    const res = await resend(
      makeReq(`/api/invites/${INVITE_ID}/resend`, { method: "POST" }),
      params({ id: INVITE_ID }),
    );
    expect(res.status).toBe(401);
  });
});
