import { randomBytes } from "node:crypto";

import { adminClient, webReplica } from "../int/clients";

/**
 * PAY-603, C69 (web LLD §5.1, §9.1): the BFF limiter against a real Redis with the production
 * `web` ACL user. Two limiter instances, each with its own connection (two replicas), share one
 * count at the production values (120 per session, 300 per IP, from the config defaults).
 */
const START = new Date("2026-11-11T10:00:05Z"); // 5 s into a window: nothing carried over

async function replicas() {
  const a = await webReplica();
  const b = await webReplica();
  const { setRedisForTests } = await import("@/lib/redis");
  const { limit } = await import("@/lib/rate-limit");
  const { config } = await import("@/lib/config");
  /** Calls `limit` through replica `which` (its own connection). */
  const via = (which: 0 | 1, ...args: Parameters<typeof limit>) => {
    setRedisForTests(which === 0 ? a : b);
    return limit(...args);
  };
  return { via, config: config() };
}

const subject = () => randomBytes(8).toString("hex"); // fresh keys on every run

describe("PAY-603 shared limits on a real Redis", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: START, toFake: ["Date"] });
  });

  // PAY-603: 120 a minute per session across two replicas; the 121st is denied.
  it("two replicas share the per-session count at 120 a minute", async () => {
    const { via, config } = await replicas();
    expect(config.BFF_RATE_LIMIT_SESSION).toBe(120);
    const s = subject();
    for (let i = 0; i < 120; i++) {
      expect(await via((i % 2) as 0 | 1, "session", s, config.BFF_RATE_LIMIT_SESSION)).toEqual({
        ok: true,
      });
    }
    const denied = await via(0, "session", s, config.BFF_RATE_LIMIT_SESSION);
    expect(denied).toEqual({ ok: false, retryAfterS: 55 });
    expect((await via(1, "session", s, config.BFF_RATE_LIMIT_SESSION)).ok).toBe(false);
    // another session is unaffected
    expect((await via(1, "session", subject(), config.BFF_RATE_LIMIT_SESSION)).ok).toBe(true);

    // The window key holds the shared count and expires (EXPIRE 120 in the MULTI).
    const admin = await adminClient();
    const w = Math.floor(START.getTime() / 60_000);
    const key = `web:rl:session:${s}:${w}`;
    expect(Number(await admin.get(key))).toBe(122);
    const ttl = await admin.ttl(key);
    expect(ttl).toBeGreaterThan(100);
    expect(ttl).toBeLessThanOrEqual(120);
  });

  // PAY-603: 300 a minute per IP across two replicas; the 301st is denied.
  it("two replicas share the per-IP count at 300 a minute", async () => {
    const { via, config } = await replicas();
    expect(config.BFF_RATE_LIMIT_IP).toBe(300);
    const ip = subject();
    for (let i = 0; i < 300; i++) {
      expect((await via((i % 2) as 0 | 1, "ip", ip, config.BFF_RATE_LIMIT_IP)).ok).toBe(true);
    }
    expect((await via(1, "ip", ip, config.BFF_RATE_LIMIT_IP)).ok).toBe(false);
  });

  // PAY-603: the sliding window carries the previous minute's count, weighted by what is left of
  // it, across replicas: halfway into the next minute, 60 of the 120 are still counted.
  it("the previous window still counts, weighted, on the other replica", async () => {
    const { via, config } = await replicas();
    const s = subject();
    for (let i = 0; i < 120; i++) await via(0, "session", s, config.BFF_RATE_LIMIT_SESSION);
    vi.setSystemTime(new Date("2026-11-11T10:01:30Z"));
    let allowed = 0;
    while ((await via(1, "session", s, config.BFF_RATE_LIMIT_SESSION)).ok) allowed++;
    expect(allowed).toBe(60);
  });
});
