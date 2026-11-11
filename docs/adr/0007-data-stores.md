# 0007. Data stores: one Postgres, one Redis; managed in uat and prod

Status: accepted 2026-11-10
Context: three schemas (identity, payroll, payouts) and small volumes. Redis holds sessions, rate limits and the payout stream, none of which may be evicted.
Options:
1. **One Postgres 16 server** with a schema and two roles per service. **One Redis 7** with `maxmemory-policy noeviction` and `appendonly yes`. In-cluster for dev, managed (for example RDS/Aurora Postgres and ElastiCache/MemoryDB) for uat and prod.
2. A separate Postgres per service: stronger isolation, but more cost and ops at this scale. The roles and grants already enforce ownership.
3. Redis split into sessions/queue and cache: there's no large cache in 0.1.0.
Recommendation: option 1.
- **Prod Redis:** prefer a durable engine (for example MemoryDB). Payout durability doesn't depend on it, though: the outbox and payroll's re-publish of non-final attempts redrive anything the stream loses (ADR 0003).
- **Redis ACLs:** one ACL user per service, limited to its own key patterns. web `~web:*`; identity `~identity:*`; payroll `~payroll:*` plus XADD on `stream:payout.requested`; payout-worker reads that stream and consumer group and writes `stream:payout.requested.dlq`. Passwords `REDIS_<SVC>_PASSWORD`.
- **ACL additions (spec R12, C27):** PING for every user; for payout-worker, XINFO on the `.dlq` stream, `XGROUP DELCONSUMER`, and write access for `XGROUP CREATE … MKSTREAM`; a read-only exporter user. Scripting stays denied for every user (`-@scripting`, spec C3).
Decision: the recommendation above, accepted by the user on 2026-11-10 (plan sign-off).
Consequences: the alerts `RedisMemoryHigh` and stream length near `MAXLEN`. Revisit if a cache is added.
