# Low-level design: identity

Source: [architecture plan](../../plan/architecture-plan.md) v1.2 (accepted 2026-11-10), [ADR 0002](../../adr/0002-sign-in-and-roles.md), [requirements](requirements.md), [high-level design](design-high-level.md), and the spec [README](README.md) "Spec resolutions" (R#) and "Contract decisions" (C#) tables, which this body already follows.
Scope: the `identity` service (`services/identity`, Python package `identity`): users, credentials, invites, password resets, roles, auth audit, the email outbox and relay, the bootstrap CLI and maintenance.
Out of scope here: BFF sessions and cookies (PAY-106, PAY-107: web LLD), shared pycommon code (settings, JWT verification, error handlers, logging, resilient client and `Guard`, `RateLimiter`, startup checks, maintenance runner: platform LLD §3), and the bootstrap SQL in `deploy/compose/init/`.

Conventions used throughout:
- "pycommon" means `libs/pycommon`; identity uses its `BaseServiceSettings`, `create_app`, JWT verifier, error format (PAY-602), logging (`log_event`), redactor, `Guard` (breaker and external-call metrics), `RateLimiter` and `caller_rate_limit`, startup checks and `MaintenanceRunner`, with the interfaces given in the platform LLD §3.
- Times are `timestamptz`, UTC. IDs are UUIDv7 from `pycommon.db.new_id()` (generated in Python, not by Postgres 16).
- Error codes are snake_case and returned as `{error: {code, message, request_id, details?}}` (PAY-602). `validation_error` details are `[{field, issue}]` and never echo the input (C14).

## 1. Modules

All paths are under `services/identity/` unless absolute.

| Module (path) | New / changed | Responsibility |
|---|---|---|
| `pyproject.toml` | new | Workspace member. Depends on `pycommon`, `fastapi`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `redis`, `argon2-cffi`, `aiosmtplib`, `jinja2`, `email-validator`. |
| Image | — | No service Dockerfile: built from the shared `deploy/docker/python-service.Dockerfile` with `PACKAGE=identity`, `SERVICE_DIR=identity`, served by `python -m pycommon.serve` with `PYCOMMON_APP=identity.main:app` on port 8000 (platform LLD §10.1). |
| `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_initial.py` | new | Alembic setup and the initial schema (§2.9). |
| `openapi.json` | new (generated) | Committed contract, written by `scripts/export_openapi.py identity`. |
| `src/identity/main.py` | new | `build_app(settings, *, instrument, clock, sender, run_relay, run_maintenance)` calls `pycommon.create_app(settings, routers=[...], migrations_script_location=..., startup_checks=IDENTITY_CHECKS, background=[email_relay, close_gauges, maintenance], on_startup=...)` and adds a `Cache-Control: no-store` middleware; `identity.main:app` is built from the environment on first access (`scripts/export_openapi.py` calls `build_app` with placeholder settings). `on_startup` creates the `PasswordHasher` (computes the dummy hash), the identity limiters (§6.4) and the relay, and points the email gauges at this app's `EmailGaugeSource` (§5.2). Background tasks: the email relay (§6.9), `close_gauges` (disposes the gauge source on stop) and, from F1, the maintenance runner (§6.10); on shutdown pycommon sets `stop` and waits, so the relay finishes its in-flight send (≤ `IDENTITY_SMTP_TIMEOUT_S`). |
| `src/identity/settings.py` | new | `CoreSettings(pycommon.BaseServiceSettings)` holds the pycommon keys plus the `IDENTITY_ARGON2_*` keys, which is all the `bootstrap_owner` CLI needs (its container has the migrator's `DB_*` keys but not the service secrets). `IdentitySettings(CoreSettings)` is the service's settings, with `service_name = "identity"` and `db_schema = "identity"`. The pycommon keys (`DB_*`, `REDIS_*`, `LOG_LEVEL`, `DOCS_ENABLED`, `SERVICE_RATE_LIMIT_USER`, `BREAKER_*`, `MAINTENANCE_INTERVAL_S`, `RETENTION_*`, …) are inherited unchanged (C1); identity adds the keys in §4.2, each field named exactly as its env var. |
| `src/identity/startup_checks.py` | new | `IDENTITY_CHECKS: list[pycommon.StartupCheck]`, the identity-specific checks passed to `create_app` (§4.3). |
| `src/identity/api/deps.py` | new | FastAPI dependencies: `public_caller` (requires `sub == "service:web"`), `user_caller` (requires a UUID `sub`), `admin_actor` (loads the actor row from the DB, requires `status = active` and `admin` in its DB roles), `client_ip` (parses `X-Client-IP`; `None` when absent). The per-caller limit is pycommon's `caller_rate_limit(exempt_subs=("service:web",))` (C17), attached to every `/v1` router. |
| `src/identity/api/schemas.py` | new | All Pydantic request and response models (§3.3). |
| `src/identity/api/errors.py` | new | One class per identity code in §3.5, each subclassing the pycommon `AppError` type with the right status (`Unauthorized`, `BadRequest`, `ValidationFailed`, `Conflict`, `NotFound`) and setting `code` and `default_message`; raised by use cases and rendered by pycommon's handler. |
| `src/identity/api/responses.py` | new | `errors(*statuses)`: the documented `ErrorBody` responses for a route's `responses=` (the common errors plus its own). |
| `src/identity/api/pagination.py` | new (F1) | `encode_cursor(id)` / `decode_cursor(cursor)` and the `Limit` / `Cursor` query types (§3.3). |
| `src/identity/runtime.py` | new | `IdentityRuntime` (settings, sessions, Redis, hasher, limits, metrics, relay, clock), built in `on_startup` and stored on `app.state`; `ip_hash()` / `email_key()` with `AUDIT_IP_HASH_KEY`; the `get_runtime` dependency. |
| `src/identity/paths.py` | new | `MIGRATIONS_DIR`, shared by the app (readiness) and the CLI (migrations-at-head check). |
| `src/identity/usecases/common.py` | new | Mapping helpers `session_user`, `user_out`, `me_out`. |
| `src/identity/api/auth.py` | new | Router: `POST /v1/auth/login`, `POST /v1/auth/password-reset/request`, `POST /v1/auth/password-reset/confirm`. HTTP mapping only. |
| `src/identity/api/invites.py` | new | Router: `POST /v1/invites/accept`, `POST /v1/invites/{id}/resend`. |
| `src/identity/api/users.py` | new | Router: `GET /v1/users/me`, `GET /v1/users`, `POST /v1/users/invite`, `PATCH /v1/users/{id}`. |
| `src/identity/api/audit.py` | new | Router: `GET /v1/audit-log`. |
| `src/identity/usecases/signin.py` | new | `login(cmd) -> SessionUser`: limiter → lookup → verify → audit, metrics, log (§6.3–6.4). |
| `src/identity/usecases/invites.py` | new | `invite_user`, `resend_invite`, `accept_invite` (one DB transaction each; §6.6). |
| `src/identity/usecases/resets.py` | new | `request_reset`, `confirm_reset`. |
| `src/identity/usecases/users.py` | new | `get_me`, `list_users`, `update_user` (self, last-admin and `roles_version` rules, §6.5). |
| `src/identity/usecases/audit.py` | new | `list_audit(cid, cursor, limit)`. |
| `src/identity/domain/roles.py` | new | `class Role(StrEnum): admin, payroll_admin, approver`; `ALL_ROLES`. |
| `src/identity/domain/passwords.py` | new | `check_password_policy(pw: str) -> None` (raises `PasswordTooWeak`); `PasswordHasher` with `async hash(pw) -> str`, `async verify(encoded: str \| None, pw) -> VerifyResult(ok: bool, needs_rehash: bool)`; runs argon2 in `asyncio.to_thread` behind a semaphore (§6.1–6.2). |
| `src/identity/domain/tokens.py` | new | `new_token() -> IssuedToken(raw: str, digest: bytes)`; `digest_of(raw: str) -> bytes \| None` (None when malformed) (§6.7). |
| `src/identity/domain/lockout.py` | new | Pure function `lock_seconds(level, base_s, max_s) -> int` (§6.4). No I/O. |
| `src/identity/domain/users.py` | new | Pure rules: `plan_user_update(actor_id, target: TargetUser, patch: PatchRequest, other_active_admin_count) -> UserChange` (`old_roles`, `new_roles` or `None` when the set is unchanged, `deactivate`, `bump`, `noop`), raising the domain `UpdateRefused(code)` with `cannot_modify_self` / `last_admin`, which the use case maps to the API errors; `roles_changed(old, new) -> bool`. |
| `src/identity/domain/email_schedule.py` | new | `retry_delay_s(attempts, base_s, max_s) -> int`; `is_exhausted(attempts, max_attempts) -> bool`. |
| `src/identity/domain/ip.py` | new | `normalise_ip(raw: str) -> str` (via `ipaddress`; raises `ValueError`); `ip_hash(ip: str, key: bytes) -> str` (HMAC-SHA256 hex, 64 chars); `email_key(email: str, key: bytes) -> str` (HMAC-SHA256 hex of the lower-cased email, for Redis keys). |
| `src/identity/domain/audit_events.py` | new | Constants for every audit event name (§2.6). |
| `src/identity/db/models.py` | new | SQLAlchemy 2.0 declarative models, `MetaData(schema="identity")` (§2). |
| `src/identity/db/repositories.py` | new | `UserRepo`, `CredentialRepo`, `InviteRepo`, `ResetRepo` (request, confirm and the relay's token issue; F1 replaced S4's `ResetTokenRepo`), `AuditRepo`, `OutboxRepo`. Every request-path query and write takes `company_id` (from the token's `cid`) as a required argument; only the relay's outbox claim and token issue, which act on rows by primary key, don't. |
| `src/identity/limits.py` | new | Login, reset and accept limits and the account lockout (§6.4), built on pycommon `RateLimiter` (`MULTI` with `INCR` and `EXPIRE … NX`) plus `SET … NX EX` (the lock), `SET … EX` (its level), `PTTL`, `GET` and `DEL`. `check_login` gates on the current failure count; `record_failure` counts a failed verify (C63); `clear_lockout` ends an account's lock after a confirmed reset or an accepted invite (C74). **No Lua** (C3). Fails **closed**: any Redis error raises `ServiceUnavailable` (503) and logs `ratelimit.backend_error`. |
| `src/identity/email/templates.py` | new | `render(template, *, name, link, expires_at) -> RenderedEmail(subject, text, html)`; `link_for(template, raw_token, base_url) -> str` (§3.9). |
| `src/identity/email/templates/{invite,password_reset}.{txt,html}.j2` | new | Jinja2 templates; HTML with autoescape. |
| `src/identity/email/smtp.py` | new | `EmailSender` protocol: `available() -> bool` (false while the breaker is open; reads the state without taking the half-open trial) and `send(msg)`. `SmtpSender.send(msg: EmailMessage) -> None` over `aiosmtplib`, run through pycommon `Guard(ProviderPolicy(name="smtp", max_retries=0, total_deadline=IDENTITY_SMTP_TIMEOUT_S, breaker_failures=BREAKER_FAILURES, breaker_reset_s=BREAKER_RESET_S))` with `operation="send"`, so it gets the breaker (`circuit_breaker_state{target="smtp"}`) and `external_requests_total` / `external_request_duration_seconds` with `provider="smtp"`. No retries inside the call: the relay owns the backoff (§6.9). |
| `src/identity/email/relay.py` | new | `EmailRelay.run(stop: asyncio.Event)`: claim with `SKIP LOCKED`, issue the token, render, send, mark (§6.9). One instance per replica. |
| `src/identity/metrics.py` | new | identity's own instruments (§5.2: `auth_signins`, `identity_emails_failed`, `identity_password_hash_seconds`; the limiter counters are pycommon's), `EmailGaugeSource` (the collection-time query for the two email gauges, on a private event-loop thread with a one-connection engine) and `register_email_gauges`. |
| `src/identity/maintenance.py` | new | `identity_tasks(settings, clock) -> list[pycommon.MaintenanceTask]` (the five §6.10 tasks, `TASK_NAMES`) and `build_runner(settings, engine, sessions, clock)`, which returns pycommon's `MaintenanceRunner(lock_name="identity:maintenance", interval_s=MAINTENANCE_INTERVAL_S)` (§6.10). |
| `src/identity/bootstrap_owner.py` | new | The CLI `python -m identity.bootstrap_owner` (§3.8). `main(argv) -> int`; `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`. |
| `tests/{unit,integration,contract}/` | new | §7. |

## 2. Data models

### 2.1 General

- Schema `identity`, owned by the migrator role `identity_migrator`. The runtime role `identity_app` gets `USAGE` on the schema and `SELECT, INSERT, UPDATE, DELETE` on its tables through the default privileges in the bootstrap SQL (`deploy/compose/init/10-identity.sql`; C2). Both connect with the pycommon keys `DB_USER` / `DB_PASSWORD` (C1): the service container sets `identity_app`, and the `migrate-identity` and `bootstrap-owner` containers set `identity_migrator`.
- Every table has `id UUID` (UUIDv7, set by the app) except `credentials` (PK `user_id`, itself a UUIDv7), plus `company_id UUID NOT NULL`, `created_at timestamptz NOT NULL DEFAULT now()` and `updated_at timestamptz NOT NULL DEFAULT now()`. `updated_at` is set by SQLAlchemy `onupdate=func.now()` (no triggers).
- Enumerations are `VARCHAR` with `CHECK` constraints, not Postgres enum types, so expand-then-contract changes don't need `ALTER TYPE`.
- Constraint names follow `pk_<table>`, `fk_<table>_<col>`, `uq_<table>_<cols>`, `ck_<table>_<rule>`, `ix_<table>_<cols>` (set via `MetaData(naming_convention=...)`).

### 2.2 `identity.users`

| Column | Type | Null | Default | Constraint / note |
|---|---|---|---|---|
| `id` | `UUID` | no | app (UUIDv7) | PK |
| `company_id` | `UUID` | no | | |
| `email` | `VARCHAR(254)` | no | | Stored as entered, trimmed. Uniqueness is case-insensitive (index below). |
| `name` | `VARCHAR(200)` | no | | 1–200 characters after trimming. |
| `roles` | `TEXT[]` | no | | `ck_users_roles`: `cardinality(roles) BETWEEN 1 AND 3 AND roles <@ ARRAY['admin','payroll_admin','approver']::text[]`. Stored sorted and de-duplicated by the app. |
| `roles_version` | `INTEGER` | no | `1` | `ck_users_roles_version`: `roles_version >= 1`. Bumped per §6.5. |
| `status` | `VARCHAR(16)` | no | `'pending'` | `ck_users_status`: `status IN ('pending','active','inactive')`. |
| `activated_at` | `timestamptz` | yes | | Set when the invite is accepted. |
| `deactivated_at` | `timestamptz` | yes | | Set on deactivation. |
| `created_by` | `UUID` | yes | | Inviting admin; `NULL` for the bootstrap owner. No FK (keeps the audit trail independent of row lifecycle). |
| `created_at`, `updated_at` | `timestamptz` | no | `now()` | |

Indexes:
- `uq_users_company_email` UNIQUE on `(company_id, lower(email))` (PAY-101 case-insensitive).
- `ix_users_company_id_id` on `(company_id, id)` (list ordering and cursor).
- Users are never deleted (plan §6).

### 2.3 `identity.credentials`

| Column | Type | Null | Default | Constraint / note |
|---|---|---|---|---|
| `user_id` | `UUID` | no | | PK; `fk_credentials_user_id` → `users(id)` `ON DELETE CASCADE` |
| `company_id` | `UUID` | no | | |
| `password_hash` | `VARCHAR(255)` | no | | Argon2id PHC string (`$argon2id$v=19$m=…,t=…,p=…$salt$hash`). |
| `password_changed_at` | `timestamptz` | no | `now()` | |
| `created_at`, `updated_at` | `timestamptz` | no | `now()` | |

### 2.4 `identity.invites`

| Column | Type | Null | Default | Constraint / note |
|---|---|---|---|---|
| `id` | `UUID` | no | app | PK. This is the `{id}` of `POST /v1/invites/{id}/resend`. |
| `company_id` | `UUID` | no | | |
| `user_id` | `UUID` | no | | `fk_invites_user_id` → `users(id)` `ON DELETE CASCADE` |
| `token_hash` | `BYTEA` | yes | | SHA-256 of the raw token bytes (32 bytes); `ck_invites_token_hash_len`: `token_hash IS NULL OR octet_length(token_hash) = 32`. `NULL` until the relay issues the token (§6.7), and after the purge (§6.10). |
| `token_issued_at` | `timestamptz` | yes | | Set each time the relay issues a token. |
| `status` | `VARCHAR(16)` | no | `'pending'` | `ck_invites_status`: `IN ('pending','accepted','revoked')`. Expiry is computed from `expires_at`, not stored. |
| `expires_at` | `timestamptz` | yes | | `NULL` until the relay sends; then `token_issued_at + INVITE_TTL_H`, reset on every send (R17, C31). `ck_invites_expiry`: `token_hash IS NULL OR expires_at IS NOT NULL`. |
| `created_by` | `UUID` | yes | | `NULL` for bootstrap. |
| `accepted_at`, `revoked_at` | `timestamptz` | yes | | |
| `created_at`, `updated_at` | `timestamptz` | no | `now()` | |

Indexes:
- `uq_invites_token_hash` UNIQUE on `(token_hash)` `WHERE token_hash IS NOT NULL`.
- `uq_invites_user_pending` UNIQUE on `(user_id)` `WHERE status = 'pending'` (at most one live invite per user).
- `ix_invites_expires_at` on `(expires_at)` (purge).

### 2.5 `identity.password_resets`

| Column | Type | Null | Default | Constraint / note |
|---|---|---|---|---|
| `id` | `UUID` | no | app | PK |
| `company_id` | `UUID` | no | | |
| `user_id` | `UUID` | no | | `fk_password_resets_user_id` → `users(id)` `ON DELETE CASCADE` |
| `token_hash` | `BYTEA` | yes | | As for invites (`ck_password_resets_token_hash_len`). |
| `token_issued_at` | `timestamptz` | yes | | |
| `status` | `VARCHAR(16)` | no | `'pending'` | `ck_password_resets_status`: `IN ('pending','used','revoked')`. |
| `expires_at` | `timestamptz` | yes | | `NULL` until the relay sends; then `token_issued_at + RESET_TTL_H`, reset on every send (R17, C31). `ck_password_resets_expiry`: `token_hash IS NULL OR expires_at IS NOT NULL`. |
| `used_at`, `revoked_at` | `timestamptz` | yes | | |
| `created_at`, `updated_at` | `timestamptz` | no | `now()` | |

Indexes: `uq_password_resets_token_hash` UNIQUE on `(token_hash)` `WHERE token_hash IS NOT NULL`; `uq_password_resets_user_pending` UNIQUE on `(user_id)` `WHERE status = 'pending'`; `ix_password_resets_expires_at` on `(expires_at)`.

### 2.6 `identity.audit_log`

| Column | Type | Null | Default | Constraint / note |
|---|---|---|---|---|
| `id` | `UUID` | no | app | PK (UUIDv7, so ordering by `id` is time ordering) |
| `company_id` | `UUID` | no | | |
| `at` | `timestamptz` | no | `now()` | |
| `actor_user_id` | `UUID` | yes | | `NULL` for anonymous flows and the bootstrap. |
| `event` | `VARCHAR(64)` | no | | One of the names below (`ck_audit_log_event`). |
| `target_user_id` | `UUID` | yes | | `NULL` when the email matched no user. |
| `ip_hash` | `CHAR(64)` | yes | | HMAC-SHA256(`AUDIT_IP_HASH_KEY`, ip) hex (PAY-NFR-08). `NULL` when no client IP was sent. |
| `details` | `JSONB` | no | `'{}'` | Non-personal context only (below). Never passwords, tokens, emails or raw IPs. |
| `created_at`, `updated_at` | `timestamptz` | no | `now()` | |

Indexes: `ix_audit_log_company_id_id` on `(company_id, id DESC)` (listing); `ix_audit_log_at` on `(at)` (purge).

The migration runs `REVOKE UPDATE, DELETE ON identity.audit_log FROM identity_app` (C2, C50), so the runtime role can append but never rewrite or delete entries. Retention purging goes through `identity.purge_audit_log(cutoff timestamptz) RETURNS integer`, a `SECURITY DEFINER` function owned by `identity_migrator` (with `SET search_path = identity, pg_temp`) that deletes only rows with `at < LEAST(cutoff, now() − interval '400 days')`: the cutoff is clamped (C63), so a caller holding `identity_app` can't use it to delete entries younger than 400 days. EXECUTE on it is granted to `identity_app` (bootstrap default privileges cover functions).

**Audit events** (`domain/audit_events.py`):

| Event | Actor | Target | `details` | Requirement |
|---|---|---|---|---|
| `user.bootstrapped` | `NULL` | owner | `{"mode": "invite" \| "password"}` | PAY-100 |
| `user.invited` | admin | invitee | `{"roles": [...], "invite_id": "<uuid>"}` | PAY-101 |
| `user.invite_resent` | admin | invitee | `{"invite_id": "<new uuid>", "revoked_invite_id": "<uuid>"}` | PAY-102 |
| `user.invite_accepted` | invitee | invitee | `{"invite_id": "<uuid>"}` | PAY-103 |
| `auth.signin.succeeded` | user | user | `{}` | PAY-104 |
| `auth.signin.failed` | `NULL` | user or `NULL` | `{"reason": "bad_password" \| "unknown_user" \| "not_active"}` | PAY-104 |
| `auth.lockout` | `NULL` | user or `NULL` | `{"lock_seconds": n, "level": n}` | PAY-104 |
| `auth.password_reset.requested` | `NULL` | user | `{"reset_id": "<uuid>"}` (only when the email matched an active user) | PAY-105 |
| `auth.password_reset.completed` | user | user | `{"reset_id": "<uuid>"}` | PAY-105 |
| `user.roles_changed` | admin | user | `{"old_roles": [...], "new_roles": [...]}` | PAY-108 |
| `user.deactivated` | admin | user | `{}` | PAY-108 |

### 2.7 `identity.email_outbox`

| Column | Type | Null | Default | Constraint / note |
|---|---|---|---|---|
| `id` | `UUID` | no | app | PK |
| `company_id` | `UUID` | no | | |
| `template` | `VARCHAR(32)` | no | | `ck_email_outbox_template`: `IN ('invite','password_reset')` |
| `recipient` | `VARCHAR(254)` | no | | The user's email at queue time. |
| `invite_id` | `UUID` | yes | | `fk_email_outbox_invite_id` → `invites(id)` `ON DELETE CASCADE` |
| `password_reset_id` | `UUID` | yes | | `fk_email_outbox_password_reset_id` → `password_resets(id)` `ON DELETE CASCADE` |
| `status` | `VARCHAR(16)` | no | `'pending'` | `ck_email_outbox_status`: `IN ('pending','sent','failed','cancelled')` |
| `attempts` | `INTEGER` | no | `0` | `ck_email_outbox_attempts`: `attempts >= 0`. Incremented at claim. |
| `next_attempt_at` | `timestamptz` | no | `now()` | Due time; also the claim lease (§6.9). |
| `last_error` | `VARCHAR(64)` | yes | | Error class or SMTP reply code only (for example `SMTPConnectError`, `smtp_451`); never message text. |
| `traceparent` | `VARCHAR(55)` | yes | | W3C trace context of the request that queued it, so the send span joins that trace. |
| `finished_at` | `timestamptz` | yes | | Set when `sent`, `failed` or `cancelled`. |
| `created_at`, `updated_at` | `timestamptz` | no | `now()` | |

- `ck_email_outbox_ref`: `(template = 'invite' AND invite_id IS NOT NULL AND password_reset_id IS NULL) OR (template = 'password_reset' AND password_reset_id IS NOT NULL AND invite_id IS NULL)`.
- Indexes: `ix_email_outbox_due` on `(next_attempt_at)` `WHERE status = 'pending'`; `ix_email_outbox_finished_at` on `(finished_at)` `WHERE status <> 'pending'`; `ix_email_outbox_invite_id` on `(invite_id)`; `ix_email_outbox_password_reset_id` on `(password_reset_id)`.
- **The rendered body, the link and the raw token are never stored** (PAY-110). The row holds only the recipient, template and a reference to the invite or reset row.

### 2.8 Login attempts and lockout: Redis, not Postgres

Decision: login counters and lockout state live in Redis under `identity:rl:*` (key layout in §6.4). No `login_attempts` table.

- The plan already assigns `identity:rl:*` to identity (plan §6, HLD §3) and lists no such table in the `identity` schema.
- The counters are short-lived (≤ 24 h) and need atomic increment-with-TTL across replicas. pycommon `RateLimiter` does that in one `MULTI` (`INCR`, `EXPIRE … NX`), and the lock uses `SET … NX EX`, with no purge job and no Lua (the `identity` ACL user keeps `-@scripting`; C3).
- They sit on the hot login path; a Postgres row per attempt would add write load exactly when credential stuffing is happening.
- The fail-closed rule (plan §2.1, PAY-603) makes the Redis dependency safe: if Redis is down, login, reset and accept return 503 rather than skipping the limits.
- The cost: a Redis data loss (FLUSHALL, failover) resets counters and active locks. That loses at most one lock window of protection, which is acceptable; the audit log in Postgres keeps the history.

### 2.9 Alembic migration plan

- `alembic.ini` points at `migrations/`. `migrations/env.py` calls `pycommon.db.run_alembic_env(Base.metadata, schema="identity", runtime_role="identity_app")`, which connects with `DB_USER` / `DB_PASSWORD` (set to `identity_migrator` and its password in the migrate container; C1), keeps `alembic_version` in schema `identity`, and afterwards revokes `INSERT, UPDATE, DELETE` on `identity.alembic_version` from `identity_app`.
- The schema itself is created by the bootstrap SQL (`deploy/compose/init/`), not by Alembic. The migration assumes it exists and is owned by the migrator.
- Revisions:
  - `0001_initial`: creates `users`, `credentials`, `invites`, `password_resets`, `audit_log`, `email_outbox`, with every constraint and index in §2.2–2.7, then `REVOKE UPDATE, DELETE ON identity.audit_log FROM identity_app` (C2, C50), and creates the `SECURITY DEFINER` function `identity.purge_audit_log(cutoff)`. Downgrade drops the tables in reverse FK order.
- Later revisions are expand-then-contract only (plan §2.3), each backward compatible with the running image.
- Run by the Compose one-shot `migrate-identity` (`alembic upgrade head`) and the Kubernetes Job `migrate-identity-<version>`, before identity starts. `/readyz` fails until `identity.alembic_version` equals the script head (PAY-605).

## 3. Interfaces

### 3.1 Callers and internal JWT

identity's keyset (`INTERNAL_JWT_PUBLIC_KEYS`) holds **web keys only**; every kid must be bound to `iss = "web"`. A token from any other issuer is rejected with 401 `invalid_token` (PAY-600).

Claims expected (verified by pycommon; EdDSA; leeway 5 s):

| Claim | Required | Value |
|---|---|---|
| `iss` | yes | `web`, and equal to the kid's bound issuer |
| `aud` | yes | `identity` |
| `sub` | yes | A user UUID, or `service:web` for the public flows |
| `cid` | yes | Company UUID. Every query is scoped by it, never by input. Public-flow tokens (`sub=service:web`) carry `cid = COMPANY_ID` (R9). |
| `roles` | yes | List of role strings; `[]` for `service:web`. identity re-reads roles from its DB for admin endpoints (§3.2). |
| `name` | user tokens | Display name (R9, C10; pycommon `Principal.name`). Absent on `service:web`. identity ignores it and reads names from its DB. Never logged. |
| `sid` | user tokens | Session hash. Absent on `service:web`. Never logged. |
| `iat`, `exp` | yes | `exp − iat` ≤ 60 s |
| `jti` | yes | UUID; not checked for replay (60 s lifetime). |

**Status codes** (C30): a missing, invalid or expired token, an unknown kid or an issuer/kid mismatch → 401 `invalid_token`; a valid token of the wrong kind for the route, or a missing role → 403 `forbidden_role`.

**Endpoints that accept `sub = service:web` with no user** (and only these): `POST /v1/auth/login`, `POST /v1/auth/password-reset/request`, `POST /v1/auth/password-reset/confirm`, `POST /v1/invites/accept`. They reject a user token, and every other endpoint rejects `service:web`, with 403 `forbidden_role`.

**Headers:**
- `Authorization: Bearer <jwt>`: all `/v1` routes.
- `X-Request-ID`: optional; echoed (pycommon).
- `X-Client-IP`: the browser's IP as the BFF derived it (`TRUSTED_PROXY_HOPS`; R18). Optional everywhere: the BFF omits it when it doesn't know the IP (C16). On the four public endpoints a request without it is limited in one shared per-IP bucket, subject `unknown` (§6.4), and audited with `ip_hash = NULL`. A value that isn't a valid IPv4/IPv6 address → 422 `validation_error`. When present it's hashed into `audit_log.ip_hash` on every route that writes an audit row: the four public endpoints, `POST /v1/users/invite` (`user.invited`), and from F1 `POST /v1/invites/{id}/resend` (`user.invite_resent`) and `PATCH /v1/users/{id}` (`user.roles_changed`, `user.deactivated`). It's trusted only because the request carries a valid `iss=web` token. Never logged raw.

### 3.2 Authorization

| Dependency | Rule | Failure |
|---|---|---|
| `public_caller` | `sub == "service:web"` | 403 `forbidden_role` |
| `user_caller` | `sub` is a UUID | 403 `forbidden_role` |
| `admin_actor` | `user_caller`, then the actor row `(cid, sub)` exists with `status = 'active'` and `'admin' = ANY(roles)` **in the DB** (JWT roles may be up to `SESSION_ROLE_RECHECK_S` stale) | 403 `forbidden_role` |
| `caller_rate_limit` | pycommon `caller_rate_limit(exempt_subs=("service:web",))` (C17): user tokens get `SERVICE_RATE_LIMIT_USER` per minute per `(iss, sub)`, with pycommon's key format (`identity:rl:caller:<sha256(caller_key)[:32]>:<now // 60>`, scope `caller`; C55). `service:web` is exempt (R18), because every anonymous visitor shares that subject; the public routes are limited per IP and per account instead (§6.4). Fails open. | 429 `rate_limited` + `Retry-After` |

### 3.3 Pydantic models (`api/schemas.py`)

All models use `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`; trimming is explicit where stated. `Email = Annotated[EmailStr, StringConstraints(max_length=254), NoNul]`. Free-text fields (`Email`, `Name`) reject NUL characters before any other check (`NoNul`, a `BeforeValidator`): 422 `validation_error` with issue `invalid_character` (C63). The `bootstrap_owner` CLI applies the same rule to `--email` and `--name` (exit 2).

```python
class Role(StrEnum): admin = "admin"; payroll_admin = "payroll_admin"; approver = "approver"
Password = Annotated[str, StringConstraints(min_length=1, max_length=256)]   # policy (≥ 12) is checked in domain → 422 password_too_weak
Token    = Annotated[str, StringConstraints(min_length=1, max_length=128)]   # format is checked in domain → 400 invite_invalid / reset_invalid
Name     = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200), NoNul]
RoleList = Annotated[list[Role], Field(min_length=1, max_length=3)]          # duplicates → 422 (validator)

class LoginIn(BaseModel):          email: Email; password: Password
class ResetRequestIn(BaseModel):   email: Email
class ResetConfirmIn(BaseModel):   token: Token; password: Password
class InviteAcceptIn(BaseModel):   token: Token; password: Password
class InviteUserIn(BaseModel):     email: Email; name: Name; roles: RoleList
class PatchUserIn(BaseModel):      roles: RoleList | None = None; status: Literal["inactive"] | None = None
                                   # model_validator: at least one field set, else 422

class SessionUser(BaseModel):      # returned by login and accept (C6); the BFF parses it with its SessionUser
    user_id: UUID; company_id: UUID; email: str; name: str          # Zod schema and maps it to Me (id = user_id)
    roles: list[Role]; roles_version: int; status: Literal["active"]

class PendingInviteOut(BaseModel):   # returned by resend
    id: UUID
    expires_at: datetime | None      # None until the relay has sent it (C31); always None from resend
    email_status: Literal["pending", "sent", "failed", "cancelled"] | None   # latest outbox row; "pending" from resend (its row is new)

class UserOut(BaseModel):
    id: UUID; email: str; name: str; roles: list[Role]; roles_version: int
    status: Literal["pending", "active", "inactive"]
    created_at: datetime; updated_at: datetime; activated_at: datetime | None; deactivated_at: datetime | None
    pending_invite_id: UUID | None   # flat (C11, R21): the user's invite with status 'pending'; None unless status == "pending"

class MeOut(UserOut):              # GET /v1/users/me (R19): roles_version, status, name and company_id for the re-check
    company_id: UUID

class UserListOut(BaseModel):      items: list[UserOut]; next_cursor: str | None
class ResetConfirmOut(BaseModel):  user_id: UUID; roles_version: int   # the BFF deletes web:user-sess:<user_id>
class AuditEntryOut(BaseModel):
    id: UUID; at: datetime; actor_user_id: UUID | None; event: str
    target_user_id: UUID | None; ip_hash: str | None; details: dict[str, Any]
class AuditListOut(BaseModel):     items: list[AuditEntryOut]; next_cursor: str | None
```

Query parameters for lists: `limit: int = Query(50, ge=1, le=100)`, `cursor: str | None = Query(None, max_length=64)`. Lists return `{items, next_cursor}` (R4, PAY-602); `next_cursor` is the opaque base64url (no padding) of the last row's 16 UUID bytes (22 characters), or `null` on the last page. Only that canonical form decodes; anything else returns 422 `validation_error` with details `[{field: "query.cursor", issue: "invalid_cursor"}]`, never echoing the value. Each page reads `limit + 1` rows to know whether another follows. Cursors never contain personal data.

### 3.4 Endpoints

Every endpoint can also return the **common errors**: 401 `invalid_token`; 413 `payload_too_large` (body > 256 KB, PAY-604); 422 `validation_error`; 500 `internal_error`; 503 `db_timeout` / `db_unavailable`. All responses carry `Cache-Control: no-store`. `operationId` is given for the generated TS client.

**POST /v1/auth/login** — `auth_login` — caller `service:web` — PAY-104
- Request `LoginIn`; header `X-Client-IP` (optional, C16).
- 200 `SessionUser` (includes `roles_version`, `status`, `company_id` and `name`; R19, C6).
- Errors: 401 `invalid_credentials` (wrong password, unknown email, user `pending` or `inactive`: same body, argon2 always runs); 429 `rate_limited` + `Retry-After` (IP limit, account limit, or account locked: R15); 503 `service_unavailable` (Redis down: fail closed); 403 `forbidden_role` (user token).

**POST /v1/auth/password-reset/request** — `auth_password_reset_request` — caller `service:web` — PAY-105
- Request `ResetRequestIn`; header `X-Client-IP` (optional).
- 202, empty body (no response schema in the contract), whether or not the email matches an active user.
- Errors: 429 `rate_limited` + `Retry-After`; 503 `service_unavailable`; 403 `forbidden_role`.

**POST /v1/auth/password-reset/confirm** — `auth_password_reset_confirm` — caller `service:web` — PAY-105
- Request `ResetConfirmIn`; header `X-Client-IP` (optional).
- 200 `ResetConfirmOut` (`user_id`, so the BFF deletes every session in `web:user-sess:<user_id>` at once; R19).
- Errors: 400 `reset_invalid` (malformed, unknown, expired, used or revoked token; or the user is not `active`); 422 `password_too_weak`; 429 `rate_limited`; 503 `service_unavailable`; 403 `forbidden_role`.
- Precedence: the token is checked before the password policy.

**POST /v1/invites/accept** — `invites_accept` — caller `service:web` — PAY-103
- Request `InviteAcceptIn`; header `X-Client-IP` (optional).
- 200 `SessionUser` (the BFF then creates the session, PAY-106).
- Errors: 400 `invite_invalid` (malformed, unknown, expired, accepted or revoked token; or the user is not `pending`); 422 `password_too_weak`; 429 `rate_limited`; 503 `service_unavailable`; 403 `forbidden_role`.
- Precedence: token before password policy.

**GET /v1/users/me** — `users_me` — any user token — PAY-107.
- 200 `MeOut` for `(cid, sub)`, whatever its status (the BFF ends the session when `status != "active"` or `roles_version` differs, and refreshes the session's `roles` and `name` otherwise; C10).
- Errors: 404 `user_not_found`; 403 `forbidden_role` (`service:web`); 429 `rate_limited`.

**GET /v1/users** — `users_list` — admin — PAY-108
- Query `limit`, `cursor`. Ordered by `id` ascending (creation order).
- 200 `UserListOut`.
- Errors: 403 `forbidden_role`; 429 `rate_limited`.

**POST /v1/users/invite** — `users_invite` — admin — PAY-101
- Request `InviteUserIn`; header `X-Client-IP` (optional; hashed into the `user.invited` audit row).
- 201 `UserOut` with `status = "pending"` and `pending_invite_id` set (its outbox row is `pending`).
- Errors: 409 `user_email_taken` (any existing user with that email in the company, any status, case-insensitive); 422 `validation_error` (empty or unknown role, duplicate roles, bad email or name); 403 `forbidden_role`; 429 `rate_limited`.

**POST /v1/invites/{id}/resend** — `invites_resend` — admin — PAY-102
- Path `id: UUID` (any invite of the user, including a revoked or expired one, so a stale UI id still works); header `X-Client-IP` (optional; hashed into the `user.invite_resent` audit row).
- 200 `PendingInviteOut` (the new invite).
- Errors: 404 `invite_not_found` (unknown id or another company); 409 `invite_not_pending` (the invite's user is not `pending`); 403 `forbidden_role`; 429 `rate_limited`.

**PATCH /v1/users/{id}** — `users_update` — admin, not on themselves — PAY-108
- Path `id: UUID` (uuid convertor, route template `/v1/users/{id:uuid}`); request `PatchUserIn`; header `X-Client-IP` (optional; hashed into the `user.roles_changed` / `user.deactivated` audit rows).
- 200 `UserOut` (unchanged and still 200 if the patch is a no-op).
- Errors: 404 `user_not_found`; 409 `cannot_modify_self` (`id == sub`); 409 `last_admin`; 422 `validation_error` (including `status` other than `"inactive"`: no reactivation in 0.1.0, R37, C25); 403 `forbidden_role`; 429 `rate_limited`.

**GET /v1/audit-log** — `audit_log_list` — admin — PAY-109
- Query `limit`, `cursor`. Newest first (`id DESC`).
- 200 `AuditListOut`.
- Errors: 403 `forbidden_role`; 429 `rate_limited`.

**GET /livez**, **GET /readyz** — no JWT (pycommon) — PAY-605. `/readyz` checks Postgres, Redis and `alembic_version` = head.

There is no public sign-up route and no HTTP route for bootstrap (ADR 0002, PAY-100).

### 3.5 Error codes

| Code | Status | Raised by |
|---|---|---|
| `invalid_token` | 401 | pycommon JWT verifier: missing, invalid or expired token, unknown kid, issuer/kid mismatch (PAY-600, C30) |
| `forbidden_role` | 403 | §3.2: wrong kind of token for the route, or missing role (PAY-601, C30) |
| `invalid_credentials` | 401 | login (PAY-104) |
| `invite_invalid` | 400 | accept (PAY-103) |
| `reset_invalid` | 400 | reset confirm (PAY-105) |
| `password_too_weak` | 422 | accept, reset confirm (PAY-103, PAY-105) |
| `user_email_taken` | 409 | invite (PAY-101) |
| `invite_not_pending` | 409 | resend (PAY-102); message "This user is no longer waiting for an invite." (the user is `active` or `inactive`) |
| `invite_not_found` | 404 | resend (PAY-602, R3) |
| `user_not_found` | 404 | me, patch (PAY-602, R3) |
| `cannot_modify_self` | 409 | patch (PAY-108) |
| `last_admin` | 409 | patch (PAY-108) |
| `rate_limited` | 429 + `Retry-After` | limiter (PAY-104, PAY-603) |
| `validation_error` | 422 | Pydantic, header and cursor validation (PAY-602, R3); `details` = `[{field, issue}]` (C14), **never the input value** (passwords and tokens must not echo back) |
| `payload_too_large` | 413 | pycommon (PAY-604) |
| `service_unavailable` | 503 | Redis down on public flows (PAY-603) |
| `db_timeout` / `db_unavailable` | 503 | pycommon DB error mapping |
| `internal_error` | 500 | pycommon (PAY-602) |

### 3.6 Events

identity publishes and consumes no stream events. Email goes through its own `email_outbox` and in-process relay.

### 3.7 Redis keys

All under the ACL pattern `~identity:*` (PAY-607). identity connects with the pycommon keys `REDIS_USERNAME=identity` and `REDIS_PASSWORD` (Compose fills it from `.env`'s `REDIS_IDENTITY_PASSWORD`; C1). The ACL user has `-@scripting`, so `EVAL` is refused (C3). Key list in §6.4.

### 3.8 `bootstrap_owner` CLI (PAY-100, PAY-609)

```text
python -m identity.bootstrap_owner --email <email> --company-id <uuid> [--name <name>] [--if-empty] [--password-env <VAR>]
```

| Argument | Required | Meaning |
|---|---|---|
| `--email` | yes | Owner email (validated like `InviteUserIn.email`). |
| `--company-id` | yes | UUID. Must equal `COMPANY_ID` (dev: `0193a000-0000-7000-8000-000000000001`, C28), else exit 2. |
| `--name` | no (default `Owner`) | Display name, 1–200 characters. |
| `--if-empty` | no | When `identity.users` already has a row, log `identity.bootstrap.skipped` (info) and exit **0** instead of 3, so Compose and the Kubernetes Job can re-run it (R16, PAY-100). |
| `--password-env` | no | Name of an env var holding the owner's password (`SEED_OWNER_PASSWORD` in the Compose one-shot). **Allowed only when `ENV=dev`** (else exit 2). The password must pass the policy (else exit 2). Creates an **active** owner and sends no email (R16). The value is never logged or echoed. |

Behaviour, in one transaction on the **migrator** credentials (`DB_USER=identity_migrator`, `DB_PASSWORD`, set by the `bootstrap-owner` container; C1, PAY-100):
1. `SELECT pg_advisory_xact_lock(hashtext('identity.bootstrap_owner'))` (two concurrent runs serialise).
2. If `SELECT 1 FROM identity.users LIMIT 1` returns a row (any company): with `--if-empty`, log `identity.bootstrap.skipped` (info) and exit 0; without it, log `identity.bootstrap.refused` (warning) and exit 3.
3. Insert the user: roles `[admin, approver, payroll_admin]`, `roles_version = 1`, `created_by = NULL`.
   - Without `--password-env`: `status = 'pending'`, an invite (`created_by = NULL`, `token_hash` and `expires_at` `NULL` until the relay sends it, §6.7) and an `email_outbox` row (`template = 'invite'`). The running service's relay sends it.
   - With `--password-env` (dev only): `status = 'active'`, `activated_at = now()`, a credential row; no invite, no email.
4. Audit `user.bootstrapped` (`details.mode` = `invite` or `password`); log `identity.bootstrap.completed` with `user_id` and `mode`.

| Exit code | Meaning |
|---|---|
| 0 | Owner created, or skipped with `--if-empty` because users exist |
| 1 | Unexpected error (logged with traceback, `identity.bootstrap.failed`) |
| 2 | Invalid arguments or configuration (argparse errors, bad email/UUID/name, `--company-id` ≠ `COMPANY_ID`, `--password-env` outside dev, the env var unset, or a weak password) |
| 3 | Refused: `identity.users` is not empty and `--if-empty` wasn't given |
| 4 | Database unreachable or migrations not at head |

The Compose one-shot runs `bootstrap_owner --if-empty --password-env SEED_OWNER_PASSWORD` (dev; PAY-609), and the Kubernetes Job runs `bootstrap_owner --if-empty` without `--password-env`, so every re-run exits 0.

### 3.9 Email templates

Rendered by `email/templates.py` with Jinja2 (`autoescape=True` for `.html.j2`, `StrictUndefined`). Each message is `multipart/alternative` (text + HTML) with `From: IDENTITY_EMAIL_FROM`, `To: <recipient>`, `Subject`, `Auto-Submitted: auto-generated`, and a generated `Message-ID`. No tracking pixels or external images.

**Link format** (token in the URL fragment, never in a query string; PAY-103, PAY-NFR-07):
- invite: `{IDENTITY_PUBLIC_BASE_URL}/accept#token={raw_token}`
- reset: `{IDENTITY_PUBLIC_BASE_URL}/reset#token={raw_token}`

`IDENTITY_PUBLIC_BASE_URL` is the web origin (R24; dev `http://127.0.0.1:${WEB_PORT}`, C12) and has no trailing slash (validator strips it). `raw_token` is base64url without padding, so it needs no escaping.

`invite.txt.j2` (subject `You're invited to PayRun`):

```text
Hello {{ name }},

You've been invited to PayRun. Open this link to set your password and activate your account:

{{ link }}

The link can be used once and expires at {{ expires_at_utc }} UTC.
If you weren't expecting this email, you can ignore it.
```

`password_reset.txt.j2` (subject `Reset your PayRun password`):

```text
Hello {{ name }},

Someone asked to reset your PayRun password. Open this link to choose a new one:

{{ link }}

The link can be used once and expires at {{ expires_at_utc }} UTC.
If you didn't ask for this, ignore this email; your password won't change.
```

The `.html.j2` versions carry the same text, with the link as an `<a href>` and as visible text. `expires_at_utc` is `expires_at` formatted `YYYY-MM-DD HH:MM`.

## 4. Configuration

### 4.1 Naming

- **pycommon keys** (C1): `IdentitySettings` inherits `BaseServiceSettings`, whose names are the contract for every Python service: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SCHEMA`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD`, plus `ENV`, `COMPANY_ID`, `LOG_LEVEL`, `DOCS_ENABLED`, `DB_POOL_SIZE`, `SERVICE_RATE_LIMIT_USER`, `BREAKER_*`, `MAINTENANCE_INTERVAL_S` and `RETENTION_*`. There is no `DATABASE_URL` or `REDIS_URL`. The migrate and bootstrap-owner containers set the same `DB_*` keys with the migrator's credentials.
- Identity keys the plan or requirements already name keep their exact names, without a prefix (`INVITE_TTL_H`, `RESET_TTL_H`, `AUTH_LOGIN_LIMIT_ACCOUNT`, `AUTH_LOGIN_LIMIT_IP`, `EMAIL_MAX_ATTEMPTS`, `AUDIT_IP_HASH_KEY`, and the unprefixed SMTP keys `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` per C49). `SMTP_STARTTLS` is the same kind of unprefixed infrastructure key, like C49's four.
- Identity-only keys this design introduces use the `IDENTITY_` prefix (C1).
- Each settings field is named exactly as its env var (pycommon settings are case-insensitive), so no aliases are needed.
- "gen" means generated by `scripts/dev-env.sh` into `.env`; "ES" means External Secrets (ADR 0008).

### 4.2 Keys

| Key | Type | dev | uat | prod | Requirement |
|---|---|---|---|---|---|
| `ENV` | `Literal["dev","uat","prod"]` | `dev` | `uat` | `prod` | PAY-605 |
| `COMPANY_ID` | UUID | `0193a000-0000-7000-8000-000000000001` (C28) | per env | per env | PAY-100, PAY-600 |
| `DB_HOST` / `DB_PORT` / `DB_NAME` | str / int / str | `postgres` / 5432 / `payrun` | managed host | managed host | PAY-606 |
| `DB_USER` | str | `identity_app` (service); `identity_migrator` (migrate-identity, bootstrap-owner) (C2) | same | same | PAY-606, PAY-100 |
| `DB_PASSWORD` | SecretStr | gen: `.env` `IDENTITY_DB_PASSWORD` (service) / `IDENTITY_DB_MIGRATOR_PASSWORD` (migrator) | ES | ES | PAY-606 |
| `DB_SCHEMA` | str | `identity` | same | same | PAY-606 |
| `DB_STATEMENT_TIMEOUT_MS` / `DB_POOL_TIMEOUT_S` | int / float | 2000 / 2 | same | same | PAY-605 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | int | 10 / 5 | same | same | PAY-NFR-05 |
| `REDIS_HOST` / `REDIS_PORT` | str / int | `redis` / 6379 | managed host | managed host | PAY-607 |
| `REDIS_USERNAME` | str | `identity` | same | same | PAY-607 |
| `REDIS_PASSWORD` | SecretStr | gen: `.env` `REDIS_IDENTITY_PASSWORD` | ES | ES | PAY-607 |
| `REDIS_SOCKET_TIMEOUT_MS` | int | 500 | 500 | 500 | PAY-603 |
| `INTERNAL_JWT_PUBLIC_KEYS` | JSON `{kid: {iss, pem}}` | gen: `.env` `IDENTITY_INTERNAL_JWT_PUBLIC_KEYS` (web key only) | ConfigMap | ConfigMap | PAY-600 |
| `AUDIT_IP_HASH_KEY` | SecretStr (≥ 32 bytes) | gen | ES | ES | PAY-NFR-08 |
| `INVITE_TTL_H` | int (hours from the send; C31) | 72 | 72 | 72 | PAY-101 |
| `RESET_TTL_H` | int (hours from the send; C31) | 1 | 1 | 1 | PAY-105 |
| `AUTH_LOGIN_LIMIT_ACCOUNT` | int (attempts per window) | 5 (raised in `compose.test.yaml`) | 5 | 5 | PAY-104 |
| `AUTH_LOGIN_LIMIT_IP` | int (attempts per window; also accept and reset confirm, R18) | 20 (raised in `compose.test.yaml`) | 20 | 20 | PAY-104, PAY-603 |
| `IDENTITY_LOGIN_WINDOW_S` | int | 900 | 900 | 900 | PAY-104 |
| `IDENTITY_LOCKOUT_BASE_S` / `IDENTITY_LOCKOUT_MAX_S` | int | 60 / 3600 | same | same | PAY-104 |
| `IDENTITY_LOCKOUT_LEVEL_TTL_S` | int | 86400 | 86400 | 86400 | PAY-104 |
| `SERVICE_RATE_LIMIT_USER` | int per min | 600 (raised in test) | 600 | 600 | PAY-603 |
| `IDENTITY_ARGON2_TIME_COST` / `_MEMORY_KIB` / `_PARALLELISM` | int | 3 / 65536 / 4 | same | same | PAY-103, PAY-104 |
| `IDENTITY_ARGON2_MAX_CONCURRENCY` | int | 4 | 4 | 4 | PAY-NFR-05 |
| `IDENTITY_PUBLIC_BASE_URL` | URL (the web origin; R24) | `http://127.0.0.1:${WEB_PORT}` = `http://127.0.0.1:4100` (C12) | `https://<uat host>` | `https://<prod host>` | PAY-103, PAY-105 |
| `IDENTITY_EMAIL_FROM` | str | `PayRun <no-reply@payrun.example.com>` | per env | per env | PAY-110 |
| `SMTP_HOST` | str | `mailpit` | `mailpit` (in-cluster) | ES | PAY-110 |
| `SMTP_PORT` | int | 1025 | 1025 | 587 | PAY-110 |
| `SMTP_STARTTLS` | bool | false | false | true | PAY-110 |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | `str \| None` / `SecretStr \| None` | unset | unset | ES | PAY-110 |
| `IDENTITY_SMTP_TIMEOUT_S` | float | 5 | 5 | 5 | PAY-110 |
| `EMAIL_MAX_ATTEMPTS` | int | 10 | 10 | 10 | PAY-110 |
| `IDENTITY_EMAIL_RETRY_BASE_S` / `IDENTITY_EMAIL_RETRY_MAX_S` | int | 30 / 1800 | same | same | PAY-110 |
| `IDENTITY_EMAIL_POLL_INTERVAL_S` | float | 2 | 2 | 2 | PAY-110 |
| `IDENTITY_EMAIL_BATCH` | int | 10 | 10 | 10 | PAY-110 |
| `IDENTITY_EMAIL_LEASE_S` | int | 60 | 60 | 60 | PAY-110 |
| `IDENTITY_GAUGE_QUERY_TIMEOUT_MS` | int | 1000 | 1000 | 1000 | PAY-110, R26 (connect and statement timeout of the email-gauge query) |
| `BREAKER_FAILURES` / `BREAKER_RESET_S` | int / float | 5 / 30 | same | same | PAY-110 |
| `MAINTENANCE_INTERVAL_S` | int | 60 | 60 | 60 | PAY-608 |
| `RETENTION_TOKENS_DAYS` | int | 7 | 7 | 7 | PAY-608 |
| `RETENTION_OUTBOX_DAYS` | int (used for sent/failed/cancelled emails) | 7 | 7 | 7 | PAY-608 |
| `IDENTITY_RETENTION_AUDIT_DAYS` | int | 400 | 400 | 400 | PAY-NFR-08 (read by the F1 `purge_audit` task; `purge_audit_log` also clamps any cutoff to ≥ 400 days old, §2.6) |
| `DOCS_ENABLED` | bool (pycommon) | true | false | false | PAY-605 |
| `LOG_LEVEL` | str (case-insensitive, upper-cased by pycommon; C24) | `INFO` | `INFO` | `INFO` | PAY-NFR-07 |
| `OTEL_SERVICE_NAME` | str | `identity` | same | same | PAY-NFR-09 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL | `http://otel-collector:4317` | per env | per env | PAY-NFR-09 |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | str (image `ENV`) | `http` | `http` | `http` | PAY-NFR-10 |
| `OTEL_TRACES_SAMPLER` | str | `parentbased_always_on` | same | same (prod sampling is the Collector's `tail_sampling`; R27, C23) | PAY-NFR-09 |

There is no `OTEL_TRACES_SAMPLER_ARG` (C23).

Pod sizing note: `IDENTITY_ARGON2_MAX_CONCURRENCY × IDENTITY_ARGON2_MEMORY_KIB` = 256 MiB of hashing memory at peak, so the identity container's memory limit must be at least 512 Mi.

### 4.3 Identity startup checks (uat and prod; PAY-605)

`IDENTITY_CHECKS` are pycommon `StartupCheck` functions, passed to `create_app(startup_checks=...)` and run by `run_startup_checks` with pycommon's common checks (missing or dev-default secrets, `dev-`/`test-` kids, `DEBUG`, `DOCS_ENABLED`). Each returns `CheckFailure(setting, reason)`:
- every kid in `INTERNAL_JWT_PUBLIC_KEYS` is bound to `iss = "web"` (reason `wrong_issuer`);
- `AUDIT_IP_HASH_KEY` is ≥ 32 bytes and doesn't start with `dev-` or `test-` (`too_short`, `dev_default`);
- `IDENTITY_PUBLIC_BASE_URL` starts with `https://` (`not_https`);
- prod only: `SMTP_STARTTLS` is true and `SMTP_USERNAME` / `SMTP_PASSWORD` are set (`missing`);
- `AUTH_LOGIN_LIMIT_ACCOUNT` ≤ 5, `AUTH_LOGIN_LIMIT_IP` ≤ 20 and `IDENTITY_LOCKOUT_BASE_S` ≥ 60, so the raised test values can't reach uat or prod (`test_value`).

As for every service (C29), pycommon logs `startup.check_failed` {setting, reason} at error for each failure, never the value, and exits with code 2 in uat and prod; in dev it logs at warning and starts.

## 5. Logging and metrics

### 5.1 Log events

Every line has pycommon's base fields (`ts`, `level`, `service`, `env`, `version`, `logger`, `event`, `msg`, `trace_id`, `span_id`, `request_id`). The table is an allow-list: `integration/test_telemetry_redaction.py` fails when an identity event carries a field it doesn't list. Identity never logs emails, names, passwords, tokens, token hashes, raw IPs, `sid` or JWTs; it logs IDs, `ip_hash` and counts.

| Event | Level | Fields | When | Requirement |
|---|---|---|---|---|
| `identity.bootstrap.completed` | info | `user_id`, `company_id`, `mode` | CLI created the owner | PAY-100 |
| `identity.bootstrap.refused` | warning | `company_id` | `users` not empty, no `--if-empty` (exit 3) | PAY-100 |
| `identity.bootstrap.skipped` | info | `company_id` | `users` not empty with `--if-empty` (exit 0; R16) | PAY-100, PAY-609 |
| `identity.bootstrap.failed` | error | `reason` (`invalid_args`, `db_unavailable`, `unexpected`); `detail` (a fixed text naming the bad argument, never a value) for `invalid_args`; `error_type` and the traceback for `unexpected` | Exit 1, 2 (bad arguments) or 4. Invalid settings exit 2 with pycommon's `settings.invalid` (field names only) instead | PAY-100 |
| `user.invited` | info | `user_id`, `invite_id`, `actor_user_id`, `roles` | Invite created | PAY-101 |
| `user.invite_resent` | info | `user_id`, `invite_id`, `revoked_invite_id`, `actor_user_id` | Resend | PAY-102 |
| `user.invite_accepted` | info | `user_id`, `invite_id` | Accept succeeded | PAY-103 |
| `invite.invalid` | info | `reason` (`malformed`, `unknown`, `expired`, `not_pending`, `user_not_pending`) | Accept rejected | PAY-103 |
| `auth.signin.succeeded` | info | `user_id`, `ip_hash`, `rehashed` (bool) | Login 200 | PAY-104 |
| `auth.signin.failed` | info | `user_id` (or absent), `reason` (`bad_password`, `unknown_user`, `not_active`), `ip_hash` | Login 401 | PAY-104 |
| `auth.signin.rate_limited` | warning | `scope` (`login_ip`, `login_account`, `login_locked`), `ip_hash`, `retry_after_s` | Login 429 | PAY-104 |
| `auth.lockout` | warning | `user_id` (or absent), `lock_level` (`level` is a base log field, C59), `lock_seconds`, `ip_hash` | An account lock starts | PAY-104 |
| `auth.password_reset.requested` | info | `matched` (bool), `user_id` (if matched), `reset_id` (if matched), `ip_hash` | Reset request 202 | PAY-105 |
| `auth.password_reset.completed` | info | `user_id`, `reset_id`, `roles_version` | Confirm 200 | PAY-105 |
| `auth.password_reset.invalid` | info | `reason` (`malformed`, `unknown`, `expired`, `not_pending`, `user_not_active`) | Confirm 400 | PAY-105 |
| `user.roles_changed` | info | `user_id`, `actor_user_id`, `old_roles`, `new_roles`, `roles_version` | PATCH roles | PAY-108 |
| `user.deactivated` | info | `user_id`, `actor_user_id`, `roles_version`, `revoked_invite_id` (if any) | PATCH status | PAY-108 |
| `user.update_refused` | info | `user_id`, `actor_user_id`, `code` (`cannot_modify_self`, `last_admin`) | PATCH 409 | PAY-108 |
| `email.queued` | info | `email_id`, `template`, `user_id` | Outbox row inserted: invite, resend, reset request (`template` `password_reset`), and the bootstrap owner's invite | PAY-101, PAY-102, PAY-105, PAY-110 |
| `email.sent` | info | `email_id`, `template`, `attempts`, `duration_ms` | SMTP accepted | PAY-110 |
| `email.send_failed` | warning | `email_id`, `template`, `attempts`, `error` (class/code), `next_attempt_in_s` | Attempt failed, will retry | PAY-110 |
| `email.failed` | error | `email_id`, `template`, `attempts`, `error` | `EMAIL_MAX_ATTEMPTS` reached | PAY-110 |
| `email.cancelled` | info | `email_id`, `template`, `reason` (`invite_not_pending`, `reset_not_pending`) | Token no longer issuable (invite or reset revoked, used or accepted; user no longer pending or active) | PAY-110 |
| `email.relay.error` | error | `error` | Unexpected error in the relay loop (it continues after 5 s) | PAY-110 |
| `metrics.gauge_query_failed` | warning | `gauge`, `error_type` (pycommon) | An email-gauge query failed or timed out; that cycle reports nothing | PAY-110, R26 |
| `ratelimit.backend_error` | warning (≤ 1 per 10 s) | `component` (`login`, `reset`, `accept`, or pycommon's caller limiter), `fail_mode`, `error_type` | Redis error in a limiter or in the lock commands (pycommon event) | PAY-603 |
| `maintenance.task.completed` / `maintenance.task.failed` | info / error | `task` (§6.10 names), `affected`, `duration_ms` / `error_type` | Each maintenance task per tick (pycommon events; C44) | PAY-608 |
| `auth.token.rejected` | warning | `reason`, `kid`, `iss` (pycommon) | JWT 401 | PAY-600 |
| `auth.forbidden` | info | `route` (the matched route template, as pycommon logs it, e.g. `/v1/users/{id:uuid}`); `reason` = `db_role` from `admin_actor` and from the in-transaction actor re-check in `PATCH /v1/users/{id}` (§6.5; same `route` label, passed in `UpdateCommand.route`). pycommon's JWT checks log `subject_kind` or `missing_roles` instead | 403 `forbidden_role` | PAY-601 |
| `http.request` | info | pycommon (route template, status, duration_ms) | Every request | PAY-NFR-09 |
| `startup.check_failed` | error (uat/prod; exit 2) / warning (dev) | `setting`, `reason` (pycommon; C29) | Startup check | PAY-605 |

### 5.2 Metrics

Names as in plan §11 and `observability.md` §5; labels bounded.

| Metric | Type | Labels | When | Requirement |
|---|---|---|---|---|
| `auth_signins_total` | counter | `outcome`: `success`, `failure`, `rate_limited` | Every login response (401 → `failure`, 429 → `rate_limited`) | PAY-104, PAY-NFR-10 |
| `rate_limited_total` | counter (pycommon) | `scope`: the `Limit.scope` values `login_ip`, `reset_ip`, `reset_account`, `accept_ip`, `caller` (counted by pycommon's limiter), plus `login_account` (the failed-attempt gate refused, §6.4) and `login_locked` (refused while the account is locked), which identity records through the login limiter's `record_denied(scope)`; identity has no counter of its own (C63) | Every 429 from identity | PAY-603 |
| `rate_limiter_errors_total` | counter (pycommon) | `component`: `login`, `reset`, `accept`, and pycommon's caller limiter | Every limiter or lock-command Redis error; lock-command errors go through the login limiter's `record_error(exc, fail_mode=closed)`, which also writes the throttled `ratelimit.backend_error` log | PAY-603 |
| `identity_emails_pending` | observable gauge, collection-time (unit `{email}`) | — | Count of `status = 'pending'` | PAY-110 |
| `identity_emails_failed_total` | counter | — | +1 each time an email becomes `failed` (§6.9; R25). Pre-initialised with `add(0)` at start | PAY-110 |
| `identity_email_oldest_pending_age_seconds` | observable gauge, collection-time (unit `s`) | — | `now − min(created_at)` of pending rows; 0 when none | PAY-110 |
| `external_requests_total` | counter (pycommon `Guard`) | `provider="smtp"`, `operation="send"`, `outcome`: pycommon's values (`ok`, `server_error`, `client_error`, `timeout`, `connect_error`, `breaker_open`) | Every SMTP send attempt | PAY-110 |
| `external_request_duration_seconds` | histogram | `provider="smtp"`, `operation="send"` | Every SMTP send | PAY-110 |
| `circuit_breaker_state` | gauge | `target="smtp"` | Breaker transitions (pycommon) | PAY-110 |
| `identity_password_hash_seconds` | histogram | `op`: `hash`, `verify` | Each argon2 call (sizing and the timing-class check) | PAY-104 |
| `http_server_request_duration_seconds` | histogram | auto (route, status) | Every request | PAY-NFR-05 |

The two email gauges are computed by **every replica** at metric collection (R26, C63) through pycommon `observable_gauge`. The callback runs one query (`SELECT count(*), COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) FROM identity.email_outbox WHERE status = 'pending'`) through `EmailGaugeSource`, which owns a private event loop on its own thread and a one-connection asyncpg engine (identity has no sync driver). The callback can therefore run on the exporter's thread or on a thread that is itself running an event loop. One result serves both gauges of a collection (reused for 1 s). The wait is bounded by `IDENTITY_GAUGE_QUERY_TIMEOUT_MS` (connect and statement timeout). A failed or timed-out query reports nothing for that cycle (pycommon logs `metrics.gauge_query_failed`). The source starts lazily at the first collection and is disposed on shutdown. pycommon keeps one callback per gauge name and meter provider, so a second app in the same process replaces the first. Alert rules use `max()` across replicas.

**`EmailOutboxStuck`** (platform rules; R25) fires on `max(identity_email_oldest_pending_age_seconds) > 900 or increase(identity_emails_failed_total[1h]) > 0`, so it clears an hour after the last failure. Alerts, promtool tests and dashboards use `auth_signins_total{outcome="failure"}` (C19). The Step 6 email drill reads the alert from alert-sink on host port 4135 (`ALERT_SINK_PORT`, test override; R31).

### 5.3 Spans worth adding manually

FastAPI, SQLAlchemy, Redis and httpx spans come from auto-instrumentation. Manual spans (`tracer = trace.get_tracer("identity")`), with attributes limited to IDs, outcomes and counts:

| Span | Attributes | Why |
|---|---|---|
| `identity.login` | `identity.outcome`, `identity.user_id` (when known) | One place to see a sign-in's limiter, lookup and verify |
| `identity.password.verify` / `identity.password.hash` | `identity.dummy` (bool) | Argon2 is the dominant cost; shows semaphore wait |
| `identity.ratelimit.check` | `identity.component` (`login`, `accept` for invite accept and reset confirm, `reset` for reset request), `identity.allowed` | Redis limiter latency. Login opens it in `usecases/signin.py`; `AuthLimits.check_accept` / `check_reset` open it in `limits.py` |
| `identity.invite.create` / `.resend` / `.accept` | `identity.user_id`, `identity.invite_id` | J1 steps |
| `identity.reset.request` / `.confirm` | `identity.user_id` (when matched) | |
| `identity.user.update` | `identity.user_id`, `identity.roles_changed`, `identity.deactivated` | |
| `identity.email.claim` | `identity.batch_size` | Relay throughput |
| `identity.email.send` | `identity.email_id`, `identity.template`, `identity.attempt` | Started as a **child of the outbox row's `traceparent`**, so the invite request and its email are one trace |

The redactor (pycommon) also runs on span attributes; no span records a request body.

## 6. Domain rules

### 6.1 Password policy (PAY-103, PAY-105)

- At least 12 characters, counted as Unicode code points; at most 256 (Pydantic, 422 `validation_error`).
- No composition rules and no trimming; the password is used exactly as sent (UTF-8 encoded for hashing).
- `check_password_policy` raises `PasswordTooWeak` → 422 `password_too_weak` when shorter than 12.
- Login does **not** apply the policy (only 1–256), so the response never hints at policy.

### 6.2 Argon2id parameters

- `argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID)` (argon2-cffi's RFC 9106 low-memory profile), from the `IDENTITY_ARGON2_*` settings.
- Hash and verify run in `asyncio.to_thread`, behind an `asyncio.Semaphore(IDENTITY_ARGON2_MAX_CONCURRENCY)`, so they never block the event loop.
- After a successful login, if `check_needs_rehash(hash)` is true (parameters changed), the new hash is written in the same transaction as the audit row (`rehashed = true` in the log).

### 6.3 Constant-time behaviour for unknown emails (PAY-104)

- At startup `PasswordHasher` computes `dummy_hash = hash(secrets.token_urlsafe(32))` with the live parameters.
- Login always does: limiter → one user query by `(company_id, lower(email))` joined to `credentials` → **one** argon2 verify:
  - user active with a credential: verify against the real hash;
  - unknown email, `pending` user (no credential), or `inactive` user: verify against `dummy_hash`, then fail.
- Every failure returns the identical 401 body (`invalid_credentials`, same message) and writes one audit row, so the DB work is the same too.
- The lockout keys use `email_key(email)` whether or not the user exists, so locking behaves the same for unknown emails.
- Reset request does the same single user lookup in both cases and always returns 202 with an empty body. A match also runs a write transaction (about 3 ms more on the dev box), so the lookup and writes are padded to a floor, `REQUEST_FLOOR_S` = 50 ms, on every path (§6.6, C74). The pad runs in a `try/finally` around the lookup and the transaction, so a path that raises (a database error, a lock or statement timeout) also takes at least the floor before its error response; only the rate limits (429) run before the timer and are not padded (`unit/test_reset_floor.py`).
- **Acceptance of the timing class** (C45): the median latency of unknown-email logins and of wrong-password logins differs by ≤ 20% (`integration/test_login.py`). The same bound holds for reset requests for unknown emails and for an active user (C74, `integration/test_password_reset.py::test_reset_request_timing`).

### 6.4 Rate limits and lockout (PAY-104, PAY-105, PAY-603)

No Lua (C3). The counters are pycommon `RateLimiter` fixed windows: one `MULTI` with `INCR key` and `EXPIRE key window_s+1 NX`, key `identity:rl:<scope>:<sha256(subject)[:32]>:<now // window_s>`, allowed while the count ≤ the limit, and `retry_after_s` = the window's end − now (≥ 1). The lock and its level are plain keys written with `SET … NX EX` and `SET … EX`.

Subjects: `ek = email_key(lower(email))`, and `ih = ip_hash(ip)`, or the literal `unknown` when the request has no `X-Client-IP` (one shared bucket; C16). `ek` and `ih` are HMAC-SHA256 with `AUDIT_IP_HASH_KEY`, and pycommon hashes the subject again, so Redis holds no emails or raw IPs.

Limiters, created in `on_startup`: `RateLimiter(redis, prefix="identity:rl", component=c)` for `c` ∈ `login`, `reset`, `accept`. Limits (all `fail_mode=closed`):

| `Limit.scope` | Limit | Window | Subject | Used by |
|---|---|---|---|---|
| `login_ip` | `AUTH_LOGIN_LIMIT_IP` | `IDENTITY_LOGIN_WINDOW_S` | `ih` | login |
| `login_account` | `AUTH_LOGIN_LIMIT_ACCOUNT` **failed** attempts (C63) | `IDENTITY_LOGIN_WINDOW_S` | `f"{ek}:{level}"` (see below) | login |
| `reset_ip` | `AUTH_LOGIN_LIMIT_IP` | `IDENTITY_LOGIN_WINDOW_S` | `ih` | reset request |
| `reset_account` | `AUTH_LOGIN_LIMIT_ACCOUNT` | `IDENTITY_LOGIN_WINDOW_S` | `ek` | reset request |
| `accept_ip` | `AUTH_LOGIN_LIMIT_IP` | `IDENTITY_LOGIN_WINDOW_S` | `ih` | invite accept and reset confirm (R18) |

Keys:

| Key | Type | TTL | Meaning |
|---|---|---|---|
| `identity:rl:<scope>:<hash>:<window>` | counter (pycommon) | `window_s + 1` | One per limit above |
| `identity:rl:login:lock:<ek>` | string: the lock's level | the lock duration | Account locked while present |
| `identity:rl:login:level:<ek>` | string: the last lock level | `IDENTITY_LOCKOUT_LEVEL_TTL_S`, rewritten on each lock | Lock level; gone after 24 h without a new lock |
| `identity:rl:caller:<hash>:<window>` | counter (pycommon `caller_rate_limit`) | 61 s | Per-user service limit (`SERVICE_RATE_LIMIT_USER`; §3.2) |

**Login algorithm.** The per-IP limit counts every attempt. The per-account limit counts **failed** attempts only (C63): the gate reads the current failure count, and a failure is added only after a failed verify. So successful sign-ins never move an account toward a lock, which settles PAY-104's "5 / 15 min" as five *failed* attempts per window.
1. `login.hit(login_ip, ih)`. Denied → 429, `Retry-After = retry_after_s`, scope `login_ip`.
2. In one pipeline: `PTTL login:lock:<ek>` and `GET login:level:<ek>` (`level` = 0 when absent). If the lock exists → 429, `Retry-After = ceil(PTTL / 1000)`, and identity increments `rate_limited_total{scope="login_locked"}`. No argon2 runs; the attempt doesn't extend the lock or count toward the next level (R15).
3. `GET` the `login_account` counter for `f"{ek}:{level}"` in the current window (pycommon's key, `identity:rl:login_account:<sha256(subject)[:32]>:<now // window_s>`). If it is ≥ `AUTH_LOGIN_LIMIT_ACCOUNT`:
   - identity increments `rate_limited_total{scope="login_account"}`;
   - `next = level + 1`; `lock_s = lock_seconds(next) = min(IDENTITY_LOCKOUT_BASE_S × 2^(next−1), IDENTITY_LOCKOUT_MAX_S)` (60, 120, 240, 480, 960, 1920, 3600, 3600 … s);
   - `SET login:lock:<ek> <next> NX EX lock_s`;
   - set → `SET login:level:<ek> <next> EX IDENTITY_LOCKOUT_LEVEL_TTL_S`; audit and log `auth.lockout`; 429, `Retry-After = lock_s`;
   - not set (a concurrent attempt locked first) → 429 with `Retry-After = ceil(PTTL / 1000)` of that lock; no second audit row.
4. Otherwise verify the password (§6.3).
5. Success: `DEL login:level:<ek>` → 200 (nothing is counted). Failure: `login.hit(login_account, f"{ek}:{level}")` (`MULTI` `INCR` + `EXPIRE … NX`, fail closed) → 401.

`SET NX` lets exactly one attempt start a lock, so concurrent attempts can't double-lock. Because the gate reads before the verify, attempts that pass the gate together can each add a failure, so a burst of concurrent failures can exceed the limit by its own size before the next attempt locks; the lock still starts on the next attempt. Because the account subject includes the level, each lock starts a fresh count. With the production values, failed attempts 1–5 in a window return 401, and the 6th attempt (whatever its password) returns 429 and locks for 60 s. After each lock the next 5 failures return 401 and the 6th locks for twice as long, up to 60 minutes. Unknown emails lock exactly the same way, so a 429 reveals nothing about whether the account exists (R15).

Limits of the fixed window (pycommon D6, accepted):
- up to 2× a limit can pass across a window edge;
- a window can't be cleared, so a success resets only the level, and failures already counted at level 0 in the current window still count until it ends (≤ `IDENTITY_LOGIN_WINDOW_S`).
- identity reads the counter key itself in step 3, so it relies on pycommon's key format (C17, C55); `unit/test_limits.py` fails if the two drift.

**Reset request:** `reset.hit(reset_ip, ih)`, then `reset.hit(reset_account, ek)`; no lock. **Accept and reset confirm:** `accept.hit(accept_ip, ih)` only.

**Clearing the lockout (C74).** After a reset confirm or an invite accept commits, `clear_lockout(ek)` deletes, in one `DEL`, `login:lock:<ek>`, `login:level:<ek>` and the current window's `login_account` counter for `f"{ek}:0"` (without that counter, the next attempt at level 0 would find the old count and lock again). So the next login starts at level 0 with no failures. Only the mailbox owner holds the reset or invite token, so this gives the owner a way out of a lock someone else caused, including the only admin. It runs after the commit, so a rolled-back confirm or accept never unlocks. It is best effort, like the `DEL` after a successful login: a Redis error is logged and counted (`ratelimit.backend_error`, `rate_limiter_errors_total{component="login"}`), the confirm or accept still succeeds, and the lock runs out on its own.

**Remaining trade-off (accepted for 0.1.0).** The lock is keyed by the email only and is checked before the password, whatever the IP. Anyone who knows an email can therefore keep that account locked by sending six wrong passwords every lock period (up to 60 min at level 7 and above), and can re-lock it right after a reset, starting again from level 1. The owner can always get back in through a reset, but an attacker who keeps hammering can keep interrupting them. `login_ip` slows a single source (20 attempts per window), not a distributed one. Keying the lock by account and IP, or letting the correct password through for a known device, would close this; it is not in 0.1.0's scope (PAY-104 asks for a per-account lock, and R15 for unknown emails to lock the same way).

**Redis errors:**
- The four public flows fail **closed** with 503 `service_unavailable`. pycommon raises it for the counters (including the failure count in step 5), and identity raises it for a Redis error in steps 2–3. Exception: a failed `DEL` after a successful login is logged and counted, and the login still returns 200, because a kept level only makes the next lock longer.
- The per-caller limit fails **open** (pycommon).
- Every error increments `rate_limiter_errors_total{component}` and logs `ratelimit.backend_error`.

### 6.5 `roles_version`, self-modification and last admin (PAY-105, PAY-107, PAY-108)

`roles_version` is incremented by exactly 1, in the same transaction, when:
- the set of roles changes (PATCH with a different set; a PATCH with the same set changes nothing and writes no audit);
- the status becomes `inactive`;
- a password reset is confirmed (so the BFF's re-check ends older sessions, PAY-105).

It is not changed by invite, resend or accept.

**PATCH /v1/users/{id}**, one transaction:
1. `admin_actor` check (DB).
2. If `id == sub` → 409 `cannot_modify_self` (checked before any lookup, so it holds even for a target in another state).
3. `SELECT id FROM users WHERE company_id = :cid AND status = 'active' AND 'admin' = ANY(roles) ORDER BY id FOR UPDATE`, then `SELECT … FROM users WHERE company_id = :cid AND id = :id FOR UPDATE` (404 `user_not_found`). Lock order is always admins first, then target.
4. Re-check that the actor is still in the locked admin set (else 403 `forbidden_role`).
5. `plan_user_update(...)`: if the target is an active admin and the change removes `admin` or sets `inactive`, and the number of **other** active admins is 0 → 409 `last_admin`. Pending admins don't count.
6. Apply: roles (sorted, de-duplicated), `status = 'inactive'` + `deactivated_at = now()`; bump `roles_version` (once per PATCH, also when roles and status change together); on deactivation also set the user's pending invite and pending password reset (if any) to `revoked` and cancel their pending outbox rows; write `user.roles_changed` and/or `user.deactivated` (with `ip_hash`). A no-op (the same role set in any order, or deactivating an inactive user) writes nothing and still returns 200. Pending and inactive users can be patched too; deactivating a pending user revokes its invite, so it can no longer be resent (409 `invite_not_pending`).

`last_admin` is defence in depth: the self rule (step 2) and the re-check (step 4) mean the actor is itself an active admin other than the target, so through the API at least one other active admin always remains. Two admins demoting or deactivating each other at once serialise on the admin locks: the first wins (200) and the second's actor is no longer in the locked set (403 `forbidden_role`). The domain rule is unit-tested (`unit/test_user_rules.py`).

**Lock order** (F1): users before invites and resets, everywhere. PATCH locks admins then the target; resend, reset request, accept and reset confirm lock the user row (`SELECT … FOR UPDATE`) before touching its invite or reset. So a deactivation racing an accept, resend or reset confirm serialises instead of deadlocking.

Only `inactive` is accepted for `status`: user reactivation is out of scope for 0.1.0 (R37, C25), so `PatchUserIn` rejects any other value with 422 `validation_error`. Inviting the email of an inactive user returns 409 `user_email_taken`.

### 6.6 Invite, resend and accept (PAY-101–103)

- **Invite** (one transaction): insert `users` (pending), `invites` (`token_hash` and `expires_at` `NULL`: both are set when the relay sends, §6.7), `email_outbox` (template `invite`, current `traceparent`), audit `user.invited` (with `ip_hash` when `X-Client-IP` was sent). A unique violation on `uq_users_company_email` → 409 `user_email_taken`.
- **Resend** (one transaction): load the invite by `(cid, id)` (404 `invite_not_found`); lock its user; if the user isn't `pending` → 409 `invite_not_pending`. Set the user's pending invite (if any) to `revoked`, cancel that invite's pending outbox rows, insert a new invite (`token_hash` and `expires_at` `NULL`) and outbox row, audit `user.invite_resent`. The old token then fails with `invite_invalid` (PAY-102). Return `PendingInviteOut` for the new invite.
- **Accept:**
  1. `digest = digest_of(token)`; malformed → 400 `invite_invalid`.
  2. `SELECT` the invite by `token_hash` and `company_id`; it must be `pending`, `expires_at > now()` (TTL from the send; C31), and its user `pending`. Else 400 `invite_invalid`.
  3. Password policy → 422 `password_too_weak`.
  4. Hash the password (outside the transaction).
  5. One transaction: `UPDATE invites SET status = 'accepted', accepted_at = now() WHERE company_id = :cid AND id = :id AND status = 'pending' AND expires_at > now() RETURNING user_id` (0 rows → 400 `invite_invalid`, which handles a concurrent double accept); `UPDATE users SET status = 'active', activated_at = now() WHERE company_id = :cid AND id = :uid AND status = 'pending'` (0 rows → rollback, 400); insert `credentials`; audit `user.invite_accepted`.
  6. After the commit, `clear_lockout(email_key(email))` (§6.4, C74): failed logins against the pending email may have locked it.
  7. Return `SessionUser` (C6).
- **Reset request:** limits `reset_ip` then `reset_account` (§6.4). One user lookup; if an active user matches, one transaction locks the user, revokes their pending reset and cancels its unsent email, inserts `password_resets` (`token_hash` and `expires_at` `NULL` until the relay sends) and an outbox row (`password_reset`), and audits `auth.password_reset.requested` (`ip_hash`). Always 202. So only the latest requested link works.
  - **Timing (C74, replaces the accepted risk of C71):** an active match also runs the locking write transaction, which made it measurably slower than an unknown, pending or inactive email, which stops after the lookup (medians 5.9 ms and 2.7 ms on the dev box). So the request pads everything after the limits (the lookup and, for a match, the transaction) to `REQUEST_FLOOR_S` = 50 ms with `asyncio.sleep` on the event loop's monotonic clock, in a `finally`, so an exception on either path (database error, lock timeout) is padded too; both paths then take about 57 ms end to end, and their medians differ by about 1% (`test_reset_request_timing`, ≤ 20% like login, C45). The limits run before the timer, and a 429 is the same for every email. Padding was chosen over dummy work, because a read-only dummy transaction still skips the commit's WAL flush that the real write pays. The floor is a constant, not a setting: if a match's transaction ever takes longer than 50 ms (a slow or overloaded database), that request is slower again, so raise the constant if the production reset-request p50 nears it. `reset_ip` and `reset_account` still bound probing.
- **Reset confirm:** like accept (400 `reset_invalid`; the reset must be `pending` with `expires_at > now()`, and the user `active`); then in one transaction lock the user, mark the reset `used` (conditional `UPDATE`; 0 rows → 400, which handles a concurrent double confirm), bump `roles_version` (only while `active`), upsert `credentials` (`INSERT … ON CONFLICT (user_id) DO UPDATE`, `password_changed_at = now()`), audit `auth.password_reset.completed`. After the commit, `clear_lockout(email_key(email))` (§6.4, C74). Return `{user_id, roles_version}`. The BFF deletes every session in `web:user-sess:<user_id>` at once (R19; identity's Redis ACL can't reach `web:*`); the `roles_version` bump makes any session it misses end at the next re-check (PAY-107).

### 6.7 Token generation and hashing

- `new_token()`: `raw_bytes = secrets.token_bytes(32)`; `raw = base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode()` (43 characters); `digest = hashlib.sha256(raw_bytes).digest()` (32 bytes).
- `digest_of(raw)`: returns `None` unless `raw` matches `^[A-Za-z0-9_-]{43}$` and decodes to 32 bytes; otherwise the SHA-256 of the decoded bytes.
- Only the digest is stored (`token_hash`). Lookup is by the unique digest index, so no secret-dependent comparison happens in Python.
- **The token is issued by the relay at send time**, not when the invite or reset row is created. The raw token exists only in the relay's memory while rendering and sending one message. That's how the outbox stores a token *reference* and never a body or a token (R17, PAY-101, PAY-110).
  - Each send attempt issues a fresh token and restarts the TTL:
    - invite: `UPDATE invites SET token_hash = :d, token_issued_at = now(), expires_at = now() + INVITE_TTL_H WHERE id = :id AND status = 'pending' AND user_id IN (SELECT id FROM users WHERE status = 'pending') RETURNING expires_at`;
    - reset: the same on `password_resets` with `RESET_TTL_H`, requiring the user to be `active`.
  - So only the most recently sent link works. If an email was delivered but the row wasn't marked `sent` (crash), the redelivered email carries the working link.
  - **Expiry runs from the send** (R17, C31): `expires_at` is `NULL` until the first send and is reset by every send. The email shows the new `expires_at`.
- Single use: accept and confirm move the row out of `pending` with a conditional `UPDATE`.

### 6.8 IP hashing

`ip_hash = hmac.new(AUDIT_IP_HASH_KEY, normalise_ip(ip).encode(), sha256).hexdigest()`, where `normalise_ip` uses `ipaddress.ip_address(...).compressed` (IPv4-mapped IPv6 converted to IPv4). Raw IPs are never stored or logged (PAY-NFR-08).

### 6.9 Email relay loop (PAY-110)

One `EmailRelay` task per replica; safe with several replicas.

```text
loop until stop:
  claim (one short transaction):
    SELECT id FROM email_outbox
     WHERE status = 'pending' AND next_attempt_at <= now()
     ORDER BY next_attempt_at
     LIMIT IDENTITY_EMAIL_BATCH
     FOR UPDATE SKIP LOCKED;
    UPDATE email_outbox SET attempts = attempts + 1,
                            next_attempt_at = now() + IDENTITY_EMAIL_LEASE_S
     WHERE id = ANY(:ids) RETURNING *;
  for each claimed row, in id order (oldest first):
    0. sender.available() is false (breaker open) → attempts = attempts − 1, next_attempt_at = now() + BREAKER_RESET_S,
       last_error 'breaker_open'; no token is issued, so expires_at and any earlier link are untouched (C31, C63); next.
    1. issue the token and set expires_at = now() + TTL (own transaction, §6.7). No row updated → status 'cancelled',
       finished_at = now(); log email.cancelled; next.
    2. render (§3.9) with the user's name and the new expires_at.
    3. send through SmtpSender (pycommon Guard: breaker, deadline IDENTITY_SMTP_TIMEOUT_S, no inner retries),
       in span identity.email.send (parent = row.traceparent).
    4. ok        → status 'sent', finished_at = now(), last_error NULL; log email.sent.
       breaker opened meanwhile (another send tripped it) → as step 0 (not counted).
       error     → if attempts >= EMAIL_MAX_ATTEMPTS: status 'failed', finished_at = now();
                   identity_emails_failed_total += 1; log email.failed
                   else next_attempt_at = now() + retry_delay_s(attempts); log email.send_failed.
                   last_error = error class or "smtp_<code>".
  if fewer than IDENTITY_EMAIL_BATCH rows were claimed: wait IDENTITY_EMAIL_POLL_INTERVAL_S (or until stop).
```

- **Backoff:** `retry_delay_s(n) = min(IDENTITY_EMAIL_RETRY_BASE_S × 2^(n−1), IDENTITY_EMAIL_RETRY_MAX_S)`, with ±10% jitter. After failures 1–9: 30, 60, 120, 240, 480, 960, 1800, 1800, 1800 s (about 2 h in total); the 10th failure marks the row `failed`.
- **Lease:** a crash after the claim leaves the row `pending` with `next_attempt_at` 60 s ahead, so another replica retries it then; the crashed attempt counts toward `EMAIL_MAX_ATTEMPTS`, which bounds poison rows.
- **Never stored:** the rendered subject and body, the link and the raw token are local variables only. `aiosmtplib` debug logging is off; the redactor masks `token`, `link` is never a log field.
- **Cancelled while in flight:** a resend, a deactivation or a newer reset request can cancel a row the relay has already claimed (`cancel_pending`, §6.5, §6.6). Every relay write to a claimed row (`finish`, `retry_in`, `release_uncounted`) is conditional on `status = 'pending'`, so the cancellation stands: a send that then succeeds leaves the row `cancelled` (`email.sent` is still logged, since the message did go out; its token is already revoked with the invite or reset), and a last-attempt failure is not counted in `identity_emails_failed_total` and logs no `email.failed`.
- **Shutdown:** the loop stops claiming when `stop` is set, finishes the message in flight, and returns. Claimed but unsent rows are retried after their lease.
- **Unexpected errors** in the loop are logged (`email.relay.error`), and the loop sleeps 5 s and continues.
- **Failed sends** set the `identity.email.send` span's status to error, with the error class only (`error.type`).
- **Expiry and failed attempts:** a token is issued for each attempt the breaker allows, so `expires_at` is set by the first *attempted* send even if that attempt fails (the link in an email that was delivered but reported as failed still works until the next attempt). C31's "set when the relay sends" means "when the relay attempts a send"; with the breaker open nothing is attempted and nothing is issued.

### 6.10 Maintenance (PAY-608)

pycommon's `MaintenanceRunner(lock_name="identity:maintenance")` ticks every `MAINTENANCE_INTERVAL_S` under `pg_try_advisory_lock(advisory_lock_key("identity:maintenance"))`, so one replica runs at a time. It runs each `MaintenanceTask` below, in this order, in its own transaction; each batched task repeats `… WHERE id IN (SELECT id … LIMIT 1000)` until a batch touches fewer than 1000 rows, and returns the rows affected. Cutoffs are computed from the app's `Clock` (`now − N days`) and passed as parameters, so tests can move time; the audit purge is one call, because the function deletes in one statement. The runner is a background task of the app (`run_maintenance=True`, the default; the integration harness turns it off and calls the tasks directly). pycommon logs `maintenance.task.completed` / `maintenance.task.failed` per task (C44) and records `maintenance_runs_total` / `maintenance_duration_seconds`.

| Task | Statement | Retention |
|---|---|---|
| `null_invite_hashes` | `UPDATE invites SET token_hash = NULL WHERE token_hash IS NOT NULL AND expires_at < now() − RETENTION_TOKENS_DAYS` | 7 days after expiry |
| `purge_invites` | `DELETE FROM invites WHERE status IN ('accepted','revoked') AND coalesce(expires_at, updated_at) < now() − RETENTION_TOKENS_DAYS` (a never-sent invite has `expires_at` `NULL`, so its revoke or accept time counts) | 7 days |
| `purge_password_resets` | `DELETE FROM password_resets WHERE coalesce(expires_at, updated_at) < now() − RETENTION_TOKENS_DAYS` | 7 days after expiry |
| `purge_emails` | `DELETE FROM email_outbox WHERE status IN ('sent','failed','cancelled') AND finished_at < now() − RETENTION_OUTBOX_DAYS` | 7 days after sent or failed |
| `purge_audit` | `SELECT identity.purge_audit_log(now() − IDENTITY_RETENTION_AUDIT_DAYS)` (the runtime role has no DELETE on `audit_log`, C50) | 400 days |

A pending user's latest invite stays `pending` (hash nulled once expired), so an admin can still resend it. An old revoked invite still carrying a hash is counted by `null_invite_hashes` and then deleted by `purge_invites` in the same tick.

Failing emails after `EMAIL_MAX_ATTEMPTS` happens in the relay (§6.9), not here.

## 7. Tests

Every test names its requirement IDs in its docstring. No test sleeps to synchronise; waits poll a condition with a deadline (`wait_until`), and time moves through `FakeClock`, time-machine or SQL. Test secrets (`STRONG_SECRET`, `AUDIT_KEY` in `tests/helpers.py`) are generated per run (C63). Unit tests run with `--disable-socket --allow-unix-socket`. Integration tests use the shared Testcontainers fixtures (`pycommon.testing`: Postgres 16 with the bootstrap SQL, Redis 7 with the ACL file), migrate as `identity_migrator`, and connect as `identity_app` (Redis user `identity`). SMTP is an in-process `aiosmtpd` server (`tests/integration/conftest.py`), which the tests can stop and restart; Mailpit itself is exercised at stack level (S8, PAY-609, e2e). Coverage: `fail_under = 85` in `pyproject.toml`, enforced on the combined unit and integration run (`--cov-append --cov-fail-under=85`).

### 7.1 `services/identity/tests/unit/`

| Test file | Covers | Requirement |
|---|---|---|
| `test_passwords.py` | Policy (11 → `PasswordTooWeak`, 12 and 256 OK, code points not bytes); argon2 parameters read from the encoded hash (`m=65536,t=3,p=4`, argon2id); `verify` with `None` uses the dummy hash and returns false; `needs_rehash` after a parameter change | PAY-103, PAY-104 |
| `test_tokens.py` | 32 random bytes, 43-char base64url, digest = SHA-256 of the raw bytes, `digest_of` rejects wrong length, padding and alphabet | PAY-101, PAY-103, PAY-105 |
| `test_lockout.py` | `lock_seconds` sequence 60, 120 … 3600 and the cap; settings defaults equal the production values (5, 20, 900, 60, 3600, 86400) | PAY-104 |
| `test_limits.py` | The login gate against an in-memory Redis double: only recorded failures count (C63), and identity's counter key equals pycommon's; the 6th attempt starts a level-1 lock; a concurrent `SET NX` winner is reported, not re-locked; Redis errors in steps 2–3 fail closed (503, counted, throttled log); a failed `DEL` after success is swallowed; `clear_lockout` deletes the lock, level and level-0 count, so the next lock is level 1 again, and a failed `DEL` is counted and logged, not raised (C74); reset requests hit `reset_ip` and `reset_account` and never lock; `check_accept` and `check_reset` open `identity.ratelimit.check` with their component (§5.3) | PAY-103, PAY-104, PAY-105, PAY-603 |
| `test_audit_events.py` | `ck_audit_log_event` and `ck_users_roles` in the models and migration 0001 list exactly `ALL_EVENTS` and the three roles | PAY-109, PAY-101 |
| `test_smtp.py` | 4xx/5xx classification; `last_error` labels carry no message text; settings reach `aiosmtplib`; connect errors open the breaker and `available()` reports it | PAY-110 |
| `test_relay_loop.py` | `email.relay.error` (class only) and the loop continues; a full batch claims again at once; stop before start | PAY-110 |
| `test_reset_floor.py` | The reset request's timing floor on error paths (C74): a failing lookup (stand-in for a database error or lock timeout) still takes at least `REQUEST_FLOOR_S` and the error propagates; a 429 from the reset limits is answered before the timer starts, so it is not padded | PAY-105, PAY-104 |
| `test_user_rules.py` | `cannot_modify_self`; `last_admin` for demote and for deactivate; pending admins don't count; `roles_version` bumps only on a real change or deactivation; role de-duplication and ordering | PAY-108, PAY-107 |
| `test_email_schedule.py` | Backoff sequence and cap; exhaustion at `EMAIL_MAX_ATTEMPTS` | PAY-110 |
| `test_email_templates.py` | Links are `{base}/accept#token=` and `{base}/reset#token=`; no `?` in the link; base URL trailing slash stripped; HTML escapes the name; subject has no token | PAY-103, PAY-105, PAY-NFR-07 |
| `test_ip.py` | IPv4/IPv6 normalisation; `ip_hash` is HMAC-SHA256 with the key (known vector), 64 hex chars, differs with another key | PAY-NFR-08 |
| `test_routes_auth.py` | Parametrised matrix: every §3.4 route (the F1 routes included) × {no token, expired, wrong `aud`, `iss=payout-worker` (kid not in the keyset), issuer/kid mismatch, garbage, `service:web`, user token} → 401 `invalid_token` for the token failures, 403 `forbidden_role` for the wrong kind of token (C30); `service:web` accepted only on the public routes. Also: `validation_error` details never echo the input (C14, C56), a bad `X-Client-IP` is 422 `header.X-Client-IP`, 413 over 256 KB, `Cache-Control: no-store`. The admin cells (user without admin → 403 from the DB check, admin → 2xx) are in `integration/test_invites.py` | PAY-600, PAY-601, PAY-602, PAY-604 |
| `test_api_errors.py` (F1) | Each identity error code in §3.5 maps to its status and shape; `PatchUserIn` rejects `status: "active"` (C25), an empty patch, and empty, unknown or duplicate roles; the reset models; cursors round-trip, and a bad cursor is 422 `query.cursor` / `invalid_cursor` without echo | PAY-602, PAY-NFR-07, PAY-108 |

The redaction and metric checks run against real Postgres, Redis and SMTP instead of fakes, so they live in the integration suite: the allow-list and no-secret checks in `integration/test_telemetry_redaction.py`; `auth_signins_total{outcome}` (all three outcomes, C19) and `rate_limited_total{scope}` (`login_ip`, `login_account`, `login_locked`) in `integration/test_login.py` and `integration/test_lockout.py`; `external_requests_total{provider="smtp"}` and `identity_emails_failed_total` (0 from start, R25) in `integration/test_email_relay.py`; `rate_limiter_errors_total{component}` in `integration/test_rate_limits.py` and `unit/test_limits.py`.
| `test_startup_checks.py` | Each §4.3 check returns `CheckFailure(setting, reason)`; in uat/prod `run_startup_checks` logs `startup.check_failed` with the setting's name and reason, never the value, and exits 2 (C29); in dev it only warns | PAY-605 |
| `test_bootstrap_args.py` | Argument parsing (including `--if-empty`) and exit code 2 cases (bad email, bad UUID, `--password-env` outside dev, unset env var, weak password, company id ≠ `COMPANY_ID`) | PAY-100, PAY-609 |

### 7.2 `services/identity/tests/integration/`

| Test file | Covers | Requirement |
|---|---|---|
| `test_migrations.py` | Upgrade to head, downgrade one step, upgrade again; `alembic_version` lives in schema `identity`; all §2 constraints exist | PAY-606 |
| `test_grants.py` | `identity_app`: `permission denied` on `payroll.*`, `payouts.*`, `bank_stub.*` and on `CREATE TABLE identity.x`; `UPDATE` and `DELETE` on `identity.audit_log` denied (C2, C50), including after re-running the bootstrap SQL; `identity.purge_audit_log(cutoff)` deletes only rows older than the cutoff, and a future or recent cutoff deletes nothing (clamped to 400 days, C63); only `identity_app` may execute it; `INSERT`/`UPDATE` on `identity.alembic_version` denied; DML on the other identity tables allowed. `identity_migrator`: no rights in other schemas | PAY-606 |
| `test_redis_acl.py` | The `identity` Redis user can use `identity:*` (`MULTI`/`INCR`/`EXPIRE NX`, `SET NX EX`, `PTTL`) but gets `NOPERM` on `web:sess:x`, on `XADD stream:payout.requested` and on `EVAL` (C3) | PAY-607 |
| `test_bootstrap_owner.py` | Runs as `identity_migrator`. Empty DB → exit 0, pending owner with all three roles, invite (`expires_at` `NULL`) and outbox row, audit `user.bootstrapped`, `email.queued` logged; second run → exit 3 and `identity.bootstrap.refused`; second run with `--if-empty` → exit 0, `identity.bootstrap.skipped`, no new rows (R16); two concurrent runs → one 0, one 3; `--if-empty --password-env SEED_OWNER_PASSWORD` in dev → active owner who can log in, no invite and no email; `--password-env` with `ENV=uat` → exit 2; DB down → exit 4 | PAY-100, PAY-609 |
| `test_invites.py` | Resend: 200 `PendingInviteOut`, the old token then `invite_invalid`, the new email's token accepts, `user.invite_resent` audited with both ids and `ip_hash`; resend before the first send cancels the unsent email; a stale (revoked) id still works; 409 `invite_not_pending`, 404 `invite_not_found` (unknown or other company), 403; concurrent resends leave one pending invite. Invite 201 with a pending user, `pending_invite_id` set (C11), a pending outbox row and `user.invited` audited with the client's `ip_hash`; 422 `invalid_character` for NUL in `email` or `name` (C63); non-admin 403 logs `auth.forbidden` (`db_role`); `GET /v1/users` shows `pending_invite_id` only for pending users; 409 `user_email_taken` (case-insensitive, concurrent duplicates); 422 for empty/unknown roles; 403 for non-admin; resend 200 revokes the old token (`invite_invalid` afterwards) and 409 `invite_not_pending` for an active user; the invite's `expires_at` is `NULL` until sent, then send time + `INVITE_TTL_H`, and moves forward on a re-send (time-machine; C31); accept: 200 `SessionUser` with `user_id`, `company_id`, `email`, `name`, `roles`, `roles_version` and `status` (C6), used / expired / unknown / revoked token → 400, 11-char password → 422, two concurrent accepts → one 200; the token travels only in the body | PAY-101, PAY-102, PAY-103, PAY-106 |
| `test_login.py` | 200 `SessionUser` for an active user; identical 401 body for wrong password, unknown email, pending and inactive users; argon2 runs on every path (`identity_password_hash_seconds{op="verify"}` count increments for each); **timing class (C45, `test_login_timing`):** with limits raised, the median latency of 30 unknown-email logins and 30 wrong-password logins differ by ≤ 20%; audit rows with `ip_hash` and no raw IP, and `ip_hash` `NULL` without `X-Client-IP`; rehash on parameter change | PAY-104, PAY-NFR-08 |
| `test_lockout.py::test_lockout_production_values` | With settings forced to the production values (5 / 20 / 900 s / 60 s / 3600 s) regardless of the test env, and a `FakeClock` at the start of a window: failed attempts 1–5 → 401, the 6th → 429 with `Retry-After` 60 and `auth.lockout` audited (level 1); the correct password during the lock → 429 (R15); two concurrent 6th attempts → one lock and one audit row; after the lock key is deleted (the lock's end), 5 more failures → 401 and the next → 429 with lock TTL 120; success deletes the level key; unknown emails lock identically; ten successful sign-ins in a row don't count, and five failures after them are still 401 (C63); `auth_signins_total` counts `success`, `failure` and `rate_limited`; 21 attempts from one IP over different emails → the 21st is 429 `scope=login_ip`; the Redis keys contain no email or IP. Also (C74): an account locked from another IP is unlocked by a confirmed reset (lock and level keys gone, the new password signs in at once, the next lock is level 1 again), and a pending email locked before its invite is unlocked by the accept | PAY-104, PAY-105 |
| `test_password_reset.py` | Request is 202 with an empty body for active, unknown, pending and inactive emails, with a reset, outbox row and audit row only for the active user; the reset's `expires_at` is set at send to send time + `RESET_TTL_H` (C31); confirm sets the password (old fails, new works), bumps `roles_version`, returns `{user_id, roles_version}` (R19); malformed / unknown / expired / used / revoked token and a no-longer-active user → 400 `reset_invalid` (token before policy); a newer request revokes the older reset and cancels its unsent email; two concurrent confirms → one 200; deactivating the user revokes a pending reset; reset requests rate-limited like login (`reset_ip`, `reset_account`); confirm shares `accept_ip` with accept (R18); **timing class (C74, `test_reset_request_timing`):** with limits raised, the median latency of 30 requests for unknown emails and 30 for an active user differs by ≤ 20% | PAY-105, PAY-107, PAY-603 |
| `test_users_me.py` | `/me` returns `MeOut` with `status`, `roles_version`, `name` and `company_id` (R19), also for an inactive user; the per-caller limit | PAY-107, PAY-603 |
| `test_users_admin.py` | List `{items, next_cursor}` with cursor and limit ≤ 100 (101 → 422, bad cursor → 422); PATCH `status: "active"` on an inactive user → 422 (C25); PATCH roles bumps `roles_version` and audits `user.roles_changed`; deactivate bumps, audits `user.deactivated` and revokes a pending invite; self-PATCH → 409 `cannot_modify_self`; last-admin guard (two admins demoting each other concurrently: one 200, one 403/409, never zero admins); actor demoted in DB but JWT still says admin → 403; a stale `admin_actor` read is refused by the in-transaction re-check (403), and both `auth.forbidden` emitters log `route` `/v1/users/{id:uuid}`; the `invite_not_pending` message after deactivation; deactivating a pending user revokes its invite and cancels its email; invite, resend and accept leave `roles_version` at 1 | PAY-107, PAY-108, PAY-601 |
| `test_audit_log.py` | Newest first, cursor paging, admin only; after a full J1 run (invite, accept, login fail and success, reset, role change) the table contains the expected events and **no** password, token, email or raw IP in any column | PAY-109, PAY-NFR-08 |
| `test_email_relay.py` | Invite → the SMTP server receives one message with a `http://127.0.0.1:<WEB_PORT>/accept#token=` link (C12) whose token accepts the invite; SMTP stopped → invite still 201, row stays pending with growing `attempts`, sent after the server restarts; with the breaker open the attempt is given back and no token is issued (`expires_at` stays `NULL`, C63); the two email gauges are 0, then the pending count and age, then 0 after sending, and a failing gauge query reports nothing and logs `metrics.gauge_query_failed` (R26); 10 failures → `failed` and `identity_emails_failed_total` +1 (R25); two relays on one table never send a row twice (SKIP LOCKED); a resent invite's old outbox row is `cancelled`; a row cancelled by a resend while its send is in flight stays `cancelled` whether the send succeeds or fails on the last attempt, with no `identity_emails_failed_total` increment and no `email.failed`; no column of `email_outbox` contains the token or the link | PAY-110 |
| `test_rate_limits.py` | Two app instances share login counts; Redis stopped → login, reset request, reset confirm and accept return 503 `service_unavailable`, while `/v1/users/me` still works (fail open) and `rate_limiter_errors_total` increments; per-caller limit 429 with `Retry-After` for a user token, while `service:web` is never caller-limited (R18, C17); accept and reset confirm share `accept_ip` at `AUTH_LOGIN_LIMIT_IP`; requests without `X-Client-IP` share the `unknown` bucket (C16) | PAY-603 |
| `test_maintenance.py` | One tick: expired token hashes nulled after `RETENTION_TOKENS_DAYS`, a pending user's latest invite kept with hash nulled; a revoked never-sent invite (`expires_at` `NULL`) purged 7 days after revocation; resets purged 7 days after expiry; sent/failed/cancelled emails purged after 7 days, pending ones kept; audit rows past 400 days purged, younger kept; each task logs `maintenance.task.completed` with its count. Cutoffs follow the `Clock` and `RETENTION_*`, and a far-future clock still can't purge young audit rows (clamp, C63); batches repeat until done; a held advisory lock makes a replica skip the tick; the app runs the runner in the background | PAY-608, PAY-NFR-08 |
| `test_readyz.py` | `/readyz` fails with the DB down, with Redis down, and when the migration isn't at head; `/livez` stays up | PAY-605 |
| `test_telemetry_redaction.py` | **The redaction test (integration):** run invite → resend → accept → login → reset (a bad then a good confirm) → role change, self-PATCH and deactivation with real Postgres, Redis and in-process SMTP, checking the §5.1 allow-list for every identity event, capturing all logs and spans; assert that the raw invite and reset tokens, the passwords, `AUDIT_IP_HASH_KEY`, `REDIS_PASSWORD`, `DB_PASSWORD`, the JWT and the raw client IP never appear | PAY-NFR-07 |

### 7.3 `services/identity/tests/contract/`

| Test file | Covers | Requirement |
|---|---|---|
| `test_openapi.py` | The exported spec equals the committed `openapi.json`; every route in §3.4 is present with its `operationId`, request model and documented error responses; no route accepts a token as a query parameter | PAY-NFR-12, PAY-103 |

The Schemathesis run against identity (no 5xx, no schema violations) is in the CI `contract` job (PAY-NFR-12). Its auth hook `scripts/ci/schemathesis_auth.py` mints a fresh token per request with `scripts/mint_token.py` (`mint(aud="identity", …)`, `cid = COMPANY_ID`), because tokens expire in 60 s (R30, C35).

## 8. Traceability

| Requirement | Module(s) | Test(s) |
|---|---|---|
| PAY-100 | `bootstrap_owner.py` | `integration/test_bootstrap_owner.py`, `unit/test_bootstrap_args.py` |
| PAY-101 | `api/users.py`, `usecases/invites.py`, `domain/tokens.py`, `email/relay.py` | `integration/test_invites.py`, `unit/test_tokens.py` |
| PAY-102 | `api/invites.py`, `usecases/invites.py` | `integration/test_invites.py` |
| PAY-103 | `api/invites.py`, `usecases/invites.py`, `domain/passwords.py`, `email/templates.py` | `integration/test_invites.py`, `unit/test_passwords.py`, `unit/test_email_templates.py`, `contract/test_openapi.py` |
| PAY-104 | `api/auth.py`, `usecases/signin.py`, `limits.py`, `domain/lockout.py`, `domain/passwords.py` | `integration/test_login.py` (`test_login_timing`), `integration/test_lockout.py::test_lockout_production_values`, `unit/test_lockout.py`, `unit/test_limits.py`, `unit/test_passwords.py` |
| PAY-105 | `api/auth.py`, `usecases/resets.py`, `limits.py`, `email/relay.py` (token and TTL at send) | `integration/test_password_reset.py` (`test_reset_request_timing`), `integration/test_lockout.py` (reset clears the lock, C74), `integration/test_rate_limits.py`, `unit/test_limits.py`, `unit/test_api_errors.py`, `unit/test_email_templates.py`, `unit/test_reset_floor.py` |
| PAY-106 | (web LLD) — identity supplies `SessionUser` | `integration/test_invites.py`, `integration/test_login.py` (response shape) |
| PAY-107 | `api/users.py` (`/me`, `MeOut`), `domain/users.py` (`roles_version`) | `integration/test_users_admin.py`, `unit/test_user_rules.py` |
| PAY-108 | `api/users.py`, `api/pagination.py`, `usecases/users.py`, `domain/users.py` | `integration/test_users_admin.py`, `integration/test_invites.py` (`pending_invite_id`), `integration/test_password_reset.py` (deactivation revokes resets), `unit/test_user_rules.py`, `unit/test_api_errors.py` |
| PAY-109 | `api/audit.py`, `api/pagination.py`, `usecases/audit.py`, `db/repositories.py` | `integration/test_audit_log.py` |
| PAY-110 | `email/relay.py`, `email/smtp.py`, `email/templates.py`, `domain/email_schedule.py`, `metrics.py` | `integration/test_email_relay.py`, `unit/test_email_schedule.py`, `unit/test_email_templates.py`, `unit/test_smtp.py`, `unit/test_relay_loop.py` |
| PAY-600 | pycommon verifier; `settings.py` keyset; `startup_checks.py` | `unit/test_routes_auth.py`, `unit/test_startup_checks.py` |
| PAY-601 | `api/deps.py` | `unit/test_routes_auth.py`, `integration/test_users_admin.py` |
| PAY-602 | `api/errors.py` (+ pycommon) | `unit/test_api_errors.py` |
| PAY-603 | `limits.py`, `api/deps.py` (pycommon `caller_rate_limit`) | `integration/test_rate_limits.py`, `integration/test_lockout.py`, `unit/test_limits.py` |
| PAY-604 | pycommon body limit (256 KB) | `unit/test_api_errors.py` (413) |
| PAY-605 | `main.py`, `startup_checks.py` | `integration/test_readyz.py`, `unit/test_startup_checks.py` |
| PAY-606 | `migrations/`, `db/models.py`, `settings.py` (`DB_*`) | `integration/test_migrations.py`, `integration/test_grants.py` |
| PAY-607 | `settings.py` (`REDIS_USERNAME=identity`) | `integration/test_redis_acl.py` |
| PAY-608 | `maintenance.py` | `integration/test_maintenance.py` |
| PAY-609 | `bootstrap_owner.py` (`--if-empty`, `--password-env`) | `integration/test_bootstrap_owner.py`, `unit/test_bootstrap_args.py` |
| PAY-NFR-07 | `email/relay.py`, `api/errors.py`, pycommon redactor | `integration/test_telemetry_redaction.py`, `unit/test_routes_auth.py` |
| PAY-NFR-08 | `domain/ip.py`, `maintenance.py`, `db/models.py` | `unit/test_ip.py`, `integration/test_audit_log.py`, `integration/test_maintenance.py` |
| PAY-NFR-09 | `email/relay.py` (`traceparent`), pycommon logging | `integration/test_telemetry_redaction.py` (`identity.email.send` span); e2e `trace.spec.ts` (web) |
| PAY-NFR-10 | `metrics.py` | `integration/test_login.py`, `integration/test_lockout.py`, `integration/test_email_relay.py` |
| PAY-NFR-12 | all; `openapi.json` | `contract/test_openapi.py`; CI coverage ≥ 85% |

## 9. Change log

- 2026-11-10: incorporated README R-table and C-table decisions (R3, R4, R9, R15, R16, R17, R18, R19, R21, R24, R25, R27, R30, R31, R37; C1, C2, C3, C6, C10, C11, C12, C14, C16, C17, C19, C23, C24, C25, C28, C29, C30, C31, C35, C44, C45).
- Resolved: SMTP keys (C49); the audit-log revoke and purge function (C50; this design §2.6, §6.10).
- 2026-11-11 (S4 alignment review, C63): `purge_audit_log` clamps its cutoff to ≥ 400 days old (§2.6). The per-account login limit counts failed attempts only (§6.4), and identity's limiter metrics go through pycommon `RateLimiter.record_denied` / `record_error` (§5.2). The relay checks the breaker before issuing a token (§6.9). The email gauges are collection-time callbacks through `EmailGaugeSource`, with the new key `IDENTITY_GAUGE_QUERY_TIMEOUT_MS` (§4.2, §5.2). `email` and `name` reject NUL (§3.3). `user.invited` carries `ip_hash` (§3.1, §6.6). The accept writes are scoped by `company_id` (§6.6). `auth.forbidden` is logged for DB-role denials, and the §5.1 field lists are an allow-list. §1 now matches the modules. Tests use in-process SMTP and generated secrets (§7), and the coverage gate is 85%.
- 2026-11-11 (F1 implementation): reset request and confirm, resend, user list and PATCH, the audit-log list and the maintenance runner are built, and the body now records what the code settled. Cursors are the canonical 22-character base64url of the UUID bytes, and a bad one is 422 `query.cursor` / `invalid_cursor` (§3.3, new `api/pagination.py`). Resend and PATCH also hash `X-Client-IP` into their audit rows (§3.1). The lock order is users before invites and resets in every flow, so accept and reset confirm now lock the user row first (§6.5). `last_admin` is documented as defence in depth, since the API's own checks always leave the actor as another active admin (§6.5). A PATCH bumps `roles_version` once even when it changes roles and status together, and a no-op writes nothing (§6.5). A reset request cancels the previous reset's unsent email (§6.6). The `auth.password_reset.invalid` reasons are listed explicitly (`user_not_active` replaces `user_not_pending`; §5.1). `ResetRepo` replaces `ResetTokenRepo`, and `domain/users.py` raises a domain `UpdateRefused` (§1). Maintenance cutoffs come from the `Clock`, batches use `id IN (SELECT … LIMIT 1000)`, and `build_app(run_maintenance=…)` is the test seam (§6.10). The §7 test rows match the files.
- 2026-11-11 (F1 review fixes): the relay's writes to a claimed outbox row are conditional on `status = 'pending'`, so a resend, deactivation or newer reset request that cancels an in-flight row wins, and a cancelled row is never counted as failed (§6.9). The reset-request timing difference is recorded as an accepted risk (§6.6). Both `auth.forbidden` emitters log the same route template (`/v1/users/{id:uuid}` for PATCH; §5.1). §5.1 `email.queued` lists resend and reset request; §3.4 lists `X-Client-IP` on resend and PATCH. The `invite_not_pending` message now reads "This user is no longer waiting for an invite." (§3.5). `identity.ratelimit.check` is also opened for the `accept` and `reset` limiters (§5.3).
- 2026-11-11 (security review LOW-2, LOW-3; C74): a confirmed password reset or an accepted invite now clears the account's login lock, level and level-0 failure count, after the commit and best effort (`AuthLimits.clear_lockout`; §1, §6.4, §6.6), so the mailbox owner can always get out of a lock someone else caused; the remaining keying trade-off (email-only lock, re-lockable after a reset) is recorded in §6.4. The reset request now pads its lookup and writes to a 50 ms floor, so an active match and an unknown email are in one timing class (medians 2.7 ms and 5.9 ms before, both about 57 ms after); this replaces the C71 accepted risk (§6.3, §6.6). New tests: `unit/test_limits.py` (clear and its Redis error), `integration/test_lockout.py` (reset confirm and invite accept unlock), `integration/test_password_reset.py::test_reset_request_timing` (§7).
- 2026-11-11 (alignment review, C74): the reset request's floor pad moved into `finally`, so a database error or lock timeout after the limits also waits out `REQUEST_FLOOR_S` and can't be told apart by speed; the 429 path still answers before the timer (§6.3, §6.6). New test: `unit/test_reset_floor.py` (§7.1).
