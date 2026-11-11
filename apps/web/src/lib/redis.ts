import "server-only";

import Redis from "ioredis";

import { config } from "./config";
import { processSingleton, setProcessSingleton } from "./process-singleton";

/**
 * The Redis port the web uses (web LLD §2.1: only GET SET DEL EXISTS INCR EXPIRE PEXPIRE PTTL
 * HSET HGET HGETALL SADD SREM SMEMBERS MULTI EXEC PING). Tests substitute a fake.
 */
export interface RedisMulti {
  hset(key: string, data: Record<string, string | number>): RedisMulti;
  expire(key: string, seconds: number): RedisMulti;
  pexpire(key: string, ms: number): RedisMulti;
  sadd(key: string, ...members: string[]): RedisMulti;
  srem(key: string, ...members: string[]): RedisMulti;
  del(...keys: string[]): RedisMulti;
  incr(key: string): RedisMulti;
  get(key: string): RedisMulti;
  exec(): Promise<[Error | null, unknown][] | null>;
}

export interface RedisLike {
  get(key: string): Promise<string | null>;
  hget(key: string, field: string): Promise<string | null>;
  hgetall(key: string): Promise<Record<string, string>>;
  hset(key: string, data: Record<string, string | number>): Promise<number>;
  del(...keys: string[]): Promise<number>;
  exists(...keys: string[]): Promise<number>;
  srem(key: string, ...members: string[]): Promise<number>;
  smembers(key: string): Promise<string[]>;
  pexpire(key: string, ms: number): Promise<number>;
  ping(): Promise<string>;
  multi(): RedisMulti;
}

/**
 * The ioredis singleton (C12 keys). `enableOfflineQueue: false`, one retry per request and a
 * `commandTimeout` of REDIS_SOCKET_TIMEOUT_MS make an outage fail fast instead of queueing.
 */
export function redis(): RedisLike {
  return processSingleton("redis", connect);
}

function connect(): RedisLike {
  const c = config();
  const r = new Redis({
    host: c.REDIS_HOST,
    port: c.REDIS_PORT,
    username: c.REDIS_USERNAME,
    password: c.REDIS_PASSWORD,
    commandTimeout: c.REDIS_SOCKET_TIMEOUT_MS,
    connectTimeout: c.REDIS_SOCKET_TIMEOUT_MS,
    enableOfflineQueue: false,
    maxRetriesPerRequest: 1,
    lazyConnect: false,
    // The `web` ACL denies INFO, which ioredis's ready check runs (web LLD §2.1).
    enableReadyCheck: false,
    retryStrategy: (times) => Math.min(times * 200, 2000),
  });
  // Errors surface on each command; this listener only stops "unhandled error event" noise.
  r.on("error", () => undefined);
  return r as unknown as RedisLike;
}

/** Runs a MULTI and throws if EXEC or any queued command failed. */
export async function execOrThrow(m: RedisMulti): Promise<unknown[]> {
  const res = await m.exec();
  if (res === null) throw new Error("redis transaction aborted");
  return res.map(([err, val]) => {
    if (err) throw err;
    return val;
  });
}

/** Tests only: substitute the client. */
export function setRedisForTests(r: RedisLike | undefined): void {
  setProcessSingleton("redis", r);
}
