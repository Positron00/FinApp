import { randomUUID } from "node:crypto";

import type Redis from "ioredis";

import { adminClient, webReplica } from "../int/clients";

/**
 * PAY-106 and PAY-607 against a real Redis with the production ACLs (deploy/compose/redis/
 * entrypoint.sh; web LLD §2.1, §4, §9.1): the session TTLs and the absolute cap as Redis applies
 * them, and the `web` user's limits.
 */
const IDLE_S = 43_200; // SESSION_IDLE_TTL_S default (12 h)
const ABS_S = 604_800; // SESSION_ABS_TTL_S default (7 days)
const T0 = new Date("2026-11-11T10:00:00Z");

async function newSession() {
  await webReplica();
  const s = await import("@/lib/session");
  const userId = randomUUID();
  const cookie = await s.createSession(
    {
      user_id: userId,
      company_id: "0193a000-0000-7000-8000-000000000001",
      name: "Int Test",
      roles: ["approver"],
      roles_version: 1,
    },
    "login",
  );
  const k = s.sessionKeyOf(cookie);
  return { s, userId, cookie, k, admin: await adminClient() };
}

describe("PAY-106 session TTLs on a real Redis", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: T0, toFake: ["Date"] });
  });

  // PAY-106: a new session expires after 12 h idle; the user index lives for the 7-day cap.
  it("creates the session with the idle TTL and the index with the absolute TTL", async () => {
    const { s, userId, cookie, k, admin } = await newSession();
    const { config } = await import("@/lib/config");
    expect([config().SESSION_IDLE_TTL_S, config().SESSION_ABS_TTL_S]).toEqual([IDLE_S, ABS_S]);
    const ttl = await admin.ttl(s.sessKey(k));
    expect(ttl).toBeGreaterThan(IDLE_S - 5);
    expect(ttl).toBeLessThanOrEqual(IDLE_S);
    const idx = await admin.ttl(s.userSessKey(userId));
    expect(idx).toBeGreaterThan(ABS_S - 5);
    expect(idx).toBeLessThanOrEqual(ABS_S);
    expect(await admin.smembers(s.userSessKey(userId))).toEqual([k]);
    // R23: Redis holds only the HMAC of the cookie, never the cookie itself
    expect(await admin.exists(`web:sess:${cookie}`)).toBe(0);
    const loaded = await s.loadSession(k);
    expect(loaded).toMatchObject({ user_id: userId, roles: ["approver"], key: k });
  });

  // PAY-106: activity slides the idle TTL back to 12 h.
  it("a touch after the touch interval restores the full idle TTL", async () => {
    const { s, k, admin } = await newSession();
    await admin.expire(s.sessKey(k), 1_000); // as if most of the idle window had passed
    vi.setSystemTime(new Date(T0.getTime() + 2 * 60_000)); // past SESSION_TOUCH_INTERVAL_S
    const sess = await s.loadSession(k);
    await s.touchSession(sess!);
    const ttl = await admin.ttl(s.sessKey(k));
    expect(ttl).toBeGreaterThan(IDLE_S - 5);
    expect(Number(await admin.hget(s.sessKey(k), "last_seen"))).toBe(Date.now());
  });

  // PAY-106: the idle TTL never runs past created_at + 7 days (the absolute cap).
  it("a touch near the absolute cap shortens the TTL to what is left of it", async () => {
    const { s, k, admin } = await newSession();
    vi.setSystemTime(new Date(T0.getTime() + (ABS_S - 100) * 1000));
    const sess = await s.loadSession(k);
    expect(sess).not.toBeNull();
    await s.touchSession(sess!);
    const pttl = await admin.pttl(s.sessKey(k));
    expect(pttl).toBeGreaterThan(95_000);
    expect(pttl).toBeLessThanOrEqual(100_000);
  });

  // PAY-106: past the absolute cap the session is gone, even if Redis still holds it.
  it("past the absolute cap the session loads as none and is deleted", async () => {
    const { s, userId, k, admin } = await newSession();
    vi.setSystemTime(new Date(T0.getTime() + (ABS_S + 1) * 1000));
    expect(await s.loadSession(k)).toBeNull();
    expect(await admin.exists(s.sessKey(k))).toBe(0);
    expect(await admin.sismember(s.userSessKey(userId), k)).toBe(0);
  });

  // PAY-107: revocation deletes every session of the user and the index.
  it("revokes every session of a user", async () => {
    const first = await newSession();
    const second = await first.s.createSession(
      {
        user_id: first.userId,
        company_id: "0193a000-0000-7000-8000-000000000001",
        name: "Int Test",
        roles: ["approver"],
        roles_version: 1,
      },
      "login",
    );
    expect(await first.s.revokeUserSessions(first.userId, "role_change")).toBe(2);
    for (const k of [first.k, first.s.sessionKeyOf(second)]) {
      expect(await first.admin.exists(first.s.sessKey(k))).toBe(0);
    }
    expect(await first.admin.exists(first.s.userSessKey(first.userId))).toBe(0);
  });
});

describe("PAY-607 web ACL user", () => {
  // PAY-607, C3: the web user can't publish payouts, read identity's keys, list keys or run Lua.
  it("gets NOPERM outside its own keys and commands", async () => {
    const web = (await webReplica()) as unknown as Redis;
    await expect(
      web.xadd("stream:payout.requested", "*", "event_id", "e", "type", "payout.requested"),
    ).rejects.toThrow(/NOPERM/);
    await expect(web.get("identity:anything")).rejects.toThrow(/NOPERM/);
    await expect(web.set("payroll:anything", "1")).rejects.toThrow(/NOPERM/);
    await expect(web.scan("0")).rejects.toThrow(/NOPERM/);
    await expect(web.eval("return 1", 0)).rejects.toThrow(/NOPERM/);
    // ...while its own namespace works (the session and limiter commands of web LLD §2.1)
    await web.set("web:int:probe", "1", "EX", 10);
    expect(await web.get("web:int:probe")).toBe("1");
  });
});
