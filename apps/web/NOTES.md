# apps/web notes

## R36 spike: the client IP with `TRUSTED_PROXY_HOPS=0` on Next 16 (S7, 2026-11-11)

**Question.** Can a Next 16 route handler see the socket peer address, so that `TRUSTED_PROXY_HOPS=0` (dev, no proxy) gives a real client IP?

**Method.** `next build` (16.4.3, `output: "standalone"`), then `node apps/web/server.js` with a temporary route that echoed the request headers. Three requests from `curl` on 127.0.0.1. The source is `node_modules/next/dist/server/base-server.js`, in `handleRequestImpl`.

| Request                                      | `X-Forwarded-For` seen by the route handler |
| -------------------------------------------- | ------------------------------------------- |
| no `X-Forwarded-For`                         | `127.0.0.1` (the socket peer)               |
| `X-Forwarded-For: 203.0.113.9`               | `203.0.113.9` (unchanged)                   |
| `X-Forwarded-For: 198.51.100.1, 203.0.113.9` | `198.51.100.1, 203.0.113.9` (unchanged)     |

**Finding.**

- Next sets the header only when it's absent: `req.headers['x-forwarded-for'] ??= socket.remoteAddress`. It **never appends** the peer.
- `NextRequest` has no `ip` property in Next 15 and later, and a route handler can't reach the socket.
- So the peer address is available only when the client sent no `X-Forwarded-For`.

**Consequences.**

1. **Dev (`hops = 0`).** The right-most `X-Forwarded-For` entry is the peer when the client sent none. That's the normal case for browsers, Playwright and k6. A client that sends its own header picks its limiter bucket. That's acceptable in dev only.
2. **The LLD §5.2 formula is off by one.** The LLD says entry `n − 1 − hops`, which assumes Next appends the peer. It doesn't, so behind one ingress that appends the client address, the client is the right-most entry (`n − 1`), not `n − 2`. With `n − 1 − hops`, uat and prod would read a client-supplied value, or `null` when the client sent none.
   - Implemented in `src/lib/client-ip.ts`: entry `n − hops` for `hops ≥ 1`, and the right-most entry (`n − 1`) for `hops = 0`.
   - `tests/lib/security.test.ts` covers hops 0, 1 and 2.
3. **uat and prod must not run with `hops = 0`.** It would trust a client-supplied value. `lib/config.ts` adds a startup check: `TRUSTED_PROXY_HOPS < 1` in uat or prod logs `config.invalid` and exits 2.

**Suggested spec text (README R36 and LLD §5.2):** "Next 16 sets `X-Forwarded-For` to the socket peer only when the request has none, and never appends. The client is entry `n − hops` (`hops ≥ 1`: each trusted proxy appends the address it saw). With `hops = 0` the right-most entry is used, which is the peer unless the client sent the header itself (dev only). uat and prod refuse `TRUSTED_PROXY_HOPS < 1` at startup."

## Other findings from S7

- **The `web` Redis ACL denies `INFO`**, and ioredis runs `INFO` for its ready check. `lib/redis.ts` sets `enableReadyCheck: false`; otherwise the first commands fail with the offline queue disabled.
- **Next bundles instrumentation, route handlers and pages separately**, so module-level singletons were duplicated (three Redis clients). `lib/process-singleton.ts` keeps the Redis client, the breakers, the re-check single-flight map and the request context on `globalThis`.
- **`AbortSignal.any` with `AbortSignal.timeout`** can be garbage-collected while an attempt is pending: the source signals hold their dependents weakly. `lib/api/resilient-fetch.ts` uses an explicit timer and AbortController instead.
- **Node 23.7 can't load undici 8 or jsdom 30** (no zstd in `node:zlib`). There's no separate 1 s connect timeout: the per-attempt read timeout bounds connect and read together. jsdom is pinned to 29.1.1.
