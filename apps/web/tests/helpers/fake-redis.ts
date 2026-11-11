import type { RedisLike, RedisMulti } from "@/lib/redis";

/**
 * An in-memory Redis for unit tests (web LLD §9.1): the `RedisLike` port with TTLs on the
 * (fakeable) clock and a fault switch. `commands` records every command name.
 */
type Val = string | Map<string, string> | Set<string>;

export class FakeRedis implements RedisLike {
  readonly data = new Map<string, Val>();
  readonly expiry = new Map<string, number>();
  readonly commands: string[] = [];
  fail = false;

  private live(key: string): boolean {
    const exp = this.expiry.get(key);
    if (exp !== undefined && exp <= Date.now()) {
      this.data.delete(key);
      this.expiry.delete(key);
    }
    return this.data.has(key);
  }

  private check(cmd: string): void {
    this.commands.push(cmd);
    if (this.fail) throw new Error("redis down (fake)");
  }

  /** Remaining TTL in ms, -1 without one, -2 when missing (PTTL). */
  pttl(key: string): number {
    if (!this.live(key)) return -2;
    const exp = this.expiry.get(key);
    return exp === undefined ? -1 : exp - Date.now();
  }

  // ---- synchronous implementations shared by direct calls and MULTI

  private _get(key: string): string | null {
    if (!this.live(key)) return null;
    const v = this.data.get(key);
    if (typeof v !== "string") throw new Error("WRONGTYPE");
    return v;
  }
  private _hash(key: string): Map<string, string> {
    if (!this.live(key)) this.data.set(key, new Map());
    const v = this.data.get(key);
    if (!(v instanceof Map)) throw new Error("WRONGTYPE");
    return v;
  }
  private _set(key: string): Set<string> {
    if (!this.live(key)) this.data.set(key, new Set());
    const v = this.data.get(key);
    if (!(v instanceof Set)) throw new Error("WRONGTYPE");
    return v;
  }
  private _hset(key: string, d: Record<string, string | number>): number {
    const h = this._hash(key);
    let added = 0;
    for (const [f, v] of Object.entries(d)) {
      if (!h.has(f)) added++;
      h.set(f, String(v));
    }
    return added;
  }
  private _del(keys: string[]): number {
    let n = 0;
    for (const k of keys) {
      if (this.live(k)) n++;
      this.data.delete(k);
      this.expiry.delete(k);
    }
    return n;
  }
  private _expireMs(key: string, ms: number): number {
    if (!this.live(key)) return 0;
    this.expiry.set(key, Date.now() + ms);
    return 1;
  }
  private _sadd(key: string, members: string[]): number {
    const s = this._set(key);
    let n = 0;
    for (const m of members) {
      if (!s.has(m)) {
        s.add(m);
        n++;
      }
    }
    return n;
  }
  private _srem(key: string, members: string[]): number {
    if (!this.live(key)) return 0;
    const s = this._set(key);
    let n = 0;
    for (const m of members) if (s.delete(m)) n++;
    return n;
  }
  private _incr(key: string): number {
    const n = Number(this._get(key) ?? "0") + 1;
    this.data.set(key, String(n));
    return n;
  }

  // ---- RedisLike

  async get(key: string) {
    this.check("get");
    return this._get(key);
  }
  async hget(key: string, field: string) {
    this.check("hget");
    if (!this.live(key)) return null;
    return this._hash(key).get(field) ?? null;
  }
  async hgetall(key: string) {
    this.check("hgetall");
    if (!this.live(key)) return {};
    return Object.fromEntries(this._hash(key));
  }
  async hset(key: string, d: Record<string, string | number>) {
    this.check("hset");
    return this._hset(key, d);
  }
  async del(...keys: string[]) {
    this.check("del");
    return this._del(keys);
  }
  async exists(...keys: string[]) {
    this.check("exists");
    return keys.filter((k) => this.live(k)).length;
  }
  async srem(key: string, ...members: string[]) {
    this.check("srem");
    return this._srem(key, members);
  }
  async smembers(key: string) {
    this.check("smembers");
    if (!this.live(key)) return [];
    return [...this._set(key)];
  }
  async pexpire(key: string, ms: number) {
    this.check("pexpire");
    return this._expireMs(key, ms);
  }
  async ping() {
    this.check("ping");
    return "PONG";
  }

  multi(): RedisMulti {
    const ops: (() => unknown)[] = [];
    const m: RedisMulti = {
      hset: (k, d) => (ops.push(() => this._hset(k, d)), m),
      expire: (k, s) => (ops.push(() => this._expireMs(k, s * 1000)), m),
      pexpire: (k, ms) => (ops.push(() => this._expireMs(k, ms)), m),
      sadd: (k, ...ms) => (ops.push(() => this._sadd(k, ms)), m),
      srem: (k, ...ms) => (ops.push(() => this._srem(k, ms)), m),
      del: (...ks) => (ops.push(() => this._del(ks)), m),
      incr: (k) => (ops.push(() => this._incr(k)), m),
      get: (k) => (ops.push(() => this._get(k)), m),
      exec: async () => {
        this.check("exec");
        return ops.map((op): [Error | null, unknown] => {
          try {
            return [null, op()];
          } catch (e) {
            return [e as Error, null];
          }
        });
      },
    };
    return m;
  }
}
