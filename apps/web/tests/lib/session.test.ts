import { createHmac } from "node:crypto";

import {
  createSession,
  deleteSessionByKey,
  loadSession,
  revokeUserSessions,
  sessKey,
  sessionKeyOf,
  touchSession,
  userSessKey,
} from "@/lib/session";

import { COMPANY_ID, SESSION_SECRET } from "../helpers/env";
import { signIn, USER_ID } from "../helpers/request";
import { t } from "../helpers/state";

const HOUR = 3600_000;

describe("session store", () => {
  // PAY-106, R23: Redis holds HMAC(SESSION_SECRET, id), never the cookie value.
  it("keys the session by the HMAC of the cookie", async () => {
    const cookie = await signIn();
    const k = createHmac("sha256", SESSION_SECRET).update(cookie).digest("hex");
    expect(sessionKeyOf(cookie)).toBe(k);
    expect(k).toMatch(/^[0-9a-f]{64}$/);
    expect([...t.redis.data.keys()].some((key) => key.includes(cookie))).toBe(false);
  });

  // PAY-106: fields including name and cid = company_id; idle TTL; the index set.
  it("creates the hash, the TTLs and the index", async () => {
    const cookie = await signIn();
    const k = sessionKeyOf(cookie);
    const s = await loadSession(k);
    expect(s).toMatchObject({
      user_id: USER_ID,
      cid: COMPANY_ID,
      name: "Olive Owner",
      roles: ["admin", "payroll_admin", "approver"],
      roles_version: 1,
      sid: k.slice(0, 32),
    });
    expect(t.redis.pttl(sessKey(k))).toBeGreaterThan(43200_000 - 1000);
    expect(t.redis.pttl(sessKey(k))).toBeLessThanOrEqual(43200_000);
    expect(t.redis.pttl(userSessKey(USER_ID))).toBeGreaterThan(604800_000 - 1000);
    expect(t.logs.events("auth.session.created")[0]).toMatchObject({ via: "login" });
    expect(t.logs.text()).not.toContain(USER_ID); // user_id_hash only
  });

  // PAY-106: rotation deletes the old session at sign-in.
  it("deletes the previous session on rotation", async () => {
    const old = await signIn();
    const fresh = await createSession(
      {
        user_id: USER_ID,
        company_id: COMPANY_ID,
        name: "O",
        roles: ["approver"],
        roles_version: 1,
      },
      "login",
      old,
    );
    expect(await loadSession(sessionKeyOf(old))).toBeNull();
    expect(await loadSession(sessionKeyOf(fresh))).not.toBeNull();
  });

  // PAY-106 §4.2: the touch runs only after SESSION_TOUCH_INTERVAL_S and caps at the absolute TTL.
  it("touches only after 60 s, and never past the absolute TTL", async () => {
    vi.useFakeTimers({ now: new Date("2026-11-10T00:00:00Z") });
    const cookie = await signIn();
    const k = sessionKeyOf(cookie);
    let s = (await loadSession(k))!;
    vi.advanceTimersByTime(30_000);
    t.redis.commands.length = 0;
    await touchSession(s);
    expect(t.redis.commands).toEqual([]);
    vi.advanceTimersByTime(31_000);
    await touchSession(s);
    expect(t.redis.commands).toContain("exec");
    expect(t.redis.pttl(sessKey(k))).toBe(43200_000);
    // 6 days 23 h in: remaining absolute time (1 h) is below the idle TTL
    vi.advanceTimersByTime(7 * 24 * HOUR - HOUR - 61_000);
    s = { ...s, last_seen: 0 };
    await t.redis.hset(sessKey(k), { last_seen: 0 });
    await t.redis.pexpire(sessKey(k), 12 * HOUR);
    await touchSession(s);
    expect(t.redis.pttl(sessKey(k))).toBe(HOUR);
  });

  // PAY-106: past the absolute TTL the session is deleted and treated as missing.
  it("expires absolutely", async () => {
    vi.useFakeTimers({ now: new Date("2026-11-10T00:00:00Z") });
    const cookie = await signIn();
    const k = sessionKeyOf(cookie);
    await t.redis.pexpire(sessKey(k), 30 * 24 * HOUR); // simulate a stale long TTL
    vi.advanceTimersByTime(604800_000 + 1);
    expect(await loadSession(k)).toBeNull();
    expect(await t.redis.exists(sessKey(k))).toBe(0);
  });

  // PAY-106: a corrupt record is deleted and treated as missing.
  it("deletes a corrupt record", async () => {
    const cookie = await signIn();
    const k = sessionKeyOf(cookie);
    await t.redis.hset(sessKey(k), { roles: "not json" });
    expect(await loadSession(k)).toBeNull();
    expect(await t.redis.exists(sessKey(k))).toBe(0);
  });

  // PAY-603: a Redis error is 503, never "no session".
  it("throws 503 on a Redis error", async () => {
    const cookie = await signIn();
    t.redis.fail = true;
    await expect(loadSession(sessionKeyOf(cookie))).rejects.toMatchObject({
      status: 503,
      code: "service_unavailable",
    });
    expect(t.logs.events("session.backend_error")).toHaveLength(1);
  });

  // PAY-106: sign-out removes the session and its index entry only.
  it("deletes one session with DEL and SREM", async () => {
    const a = await signIn();
    const b = await signIn();
    await deleteSessionByKey(sessionKeyOf(a));
    expect(await t.redis.smembers(userSessKey(USER_ID))).toEqual([sessionKeyOf(b)]);
  });

  // PAY-107, PAY-105: revocation deletes every session and the index.
  it("revokes every session of a user", async () => {
    const a = await signIn();
    const b = await signIn();
    expect(await revokeUserSessions(USER_ID, "password_reset")).toBe(2);
    expect(await loadSession(sessionKeyOf(a))).toBeNull();
    expect(await loadSession(sessionKeyOf(b))).toBeNull();
    expect(await t.redis.exists(userSessKey(USER_ID))).toBe(0);
    expect(t.logs.events("auth.session.revoked")[0]).toMatchObject({
      reason: "password_reset",
      count: 2,
    });
  });

  // PAY-107: a Redis error while revoking is logged and is a 503.
  it("logs revoke_failed when Redis fails", async () => {
    t.redis.fail = true;
    await expect(revokeUserSessions(USER_ID, "role_change")).rejects.toMatchObject({ status: 503 });
    expect(t.logs.events("auth.session.revoke_failed")).toHaveLength(1);
  });

  // PAY-106 §4.2: creation prunes index members whose session expired.
  it("prunes stale index members", async () => {
    await t.redis.multi().sadd(userSessKey(USER_ID), "stale-k").exec();
    const cookie = await signIn();
    expect(await t.redis.smembers(userSessKey(USER_ID))).toEqual([sessionKeyOf(cookie)]);
  });

  // PAY-106 §4.1 step 5: a Redis failure at creation is 503.
  it("throws 503 when creation fails", async () => {
    t.redis.fail = true;
    await expect(signIn()).rejects.toMatchObject({ status: 503 });
  });
});
