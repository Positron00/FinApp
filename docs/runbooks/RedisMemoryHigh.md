# Runbook: RedisMemoryHigh

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | redis |
| **Alert** | `max(redis_memory_used_bytes / (redis_memory_max_bytes > 0)) > 0.8` for 5 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-NFR-10; Redis runs with `maxmemory 256mb` and `maxmemory-policy noeviction` (`deploy/compose/redis/redis.conf`) |
| **Dashboard** | Platform, `http://127.0.0.1:4101/d/payrun-platform` ("Memory used vs maxmemory") |

## What it means

Redis uses more than 80% of `maxmemory`. With `noeviction`, Redis never drops keys; once full, every write fails (`OOM command not allowed`). The rule ignores a Redis without a limit (`maxmemory 0`).

## Impact

None yet. When full: the outbox relay cannot XADD (OutboxStuck), sessions and rate-limit counters cannot be written (sign-in fails; RateLimiterErrors), and the worker cannot write to the DLQ.

## First checks

1. **Memory and growth:**

   ```promql
   max(redis_memory_used_bytes) / max(redis_memory_max_bytes)
   ```

2. **The streams** (usually the biggest keys):

   ```promql
   max by (stream) (redis_stream_length)
   ```

   `stream:payout.requested` is capped at about 100000 entries and the DLQ at about 10000.
3. **Clients and keys:** connected clients on the Platform dashboard; in dev, as the admin user, `redis-cli INFO memory` and `redis-cli --bigkeys`.
4. **Evictions must stay 0:**

   ```promql
   sum(redis_evicted_keys_total)
   ```

## Likely causes

- The payout stream near `MAXLEN` with large envelopes, or a DLQ that was never drained (PayoutDLQ).
- Many sessions or rate-limit windows after a traffic spike (they expire on their own).
- `maxmemory` too small for the environment.

## Mitigation

1. Drain the DLQ per the PayoutDLQ runbook once its payouts are resolved.
2. If the stream is long but fully acknowledged, it trims itself; nothing to do.
3. Raise `maxmemory` (and the container or managed-Redis size) if normal use needs it. Never switch to an evicting policy: it would silently drop stream entries and sessions.

## Confirm recovery

- Usage falls below 80% and the alert resolves:

  ```promql
  max(redis_memory_used_bytes / (redis_memory_max_bytes > 0))
  ```
