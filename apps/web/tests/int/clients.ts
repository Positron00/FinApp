import Redis from "ioredis";
import { inject } from "vitest";

import { redis, type RedisLike, setRedisForTests } from "@/lib/redis";

/** Real clients opened by a test; `setup.ts` closes them after each test. */
const open: Redis[] = [];

async function ready(r: Redis): Promise<Redis> {
  // enableOfflineQueue is off in production, so wait for the connection before the first command
  if (r.status !== "ready") await new Promise<void>((res) => r.once("ready", () => res()));
  return r;
}

/**
 * A new production client (src/lib/redis.ts `connect`, the `web` ACL user): a "replica" with its
 * own connection. Installs it as the process singleton and returns it.
 */
export async function webReplica(): Promise<RedisLike> {
  setRedisForTests(undefined);
  const r = redis();
  open.push(r as unknown as Redis);
  await ready(r as unknown as Redis);
  return r;
}

/** The `default` (admin) user, to inspect TTLs and keys the web user can't. */
export async function adminClient(): Promise<Redis> {
  const c = inject("redis");
  const r = new Redis({
    host: c.host,
    port: c.port,
    username: "default",
    password: c.passwords.admin,
  });
  open.push(r);
  return ready(r);
}

export async function closeClients(): Promise<void> {
  await Promise.all(open.splice(0).map((r) => r.quit().catch(() => r.disconnect())));
}
