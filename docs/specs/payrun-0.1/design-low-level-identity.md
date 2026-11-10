# Low-level design: identity

Source: [architecture plan](../../plan/architecture-plan.md) v1.2 (accepted 2026-11-10), [ADR 0002](../../adr/0002-sign-in-and-roles.md), [requirements](requirements.md), [high-level design](design-high-level.md).
Scope: the `identity` service (`services/identity`, Python package `identity`): users, credentials, invites, password resets, roles, auth audit, the email outbox and relay, the bootstrap CLI and maintenance.
Out of scope here: BFF sessions and cookies (PAY-106, PAY-107: web LLD), shared pycommon code (JWT verification, error handlers, logging, resilient client, maintenance runner), and the bootstrap SQL in `deploy/compose/init/`.

Conventions used throughout:
- "pycommon" means `libs/pycommon`; identity uses its `create_app`, JWT verifier, error format (PAY-602), logging (`log_event`), redactor, breaker, rate limiter and maintenance runner. Where identity needs something pycommon may not provide, it's listed in §9.
- Times are `timestamptz`, UTC. IDs are UUIDv7 from `pycommon.ids.new_id()` (generated in Python, not by Postgres 16).
- Error codes are snake_case and returned as `{error: {code, message, request_id, details?}}` (PAY-602).

## 1. Modules

All paths are under `services/identity/` unless absolute.

| Module (path) | New / changed | Responsibility |
|---|---|---|
| `pyproject.toml` | new | Workspace member. Depends on `pycommon`, `fastapi`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `redis`, `argon2-cffi`, `aiosmtplib`, `jinja2`, `email-validator`. |
| `Dockerfile` | new | Non-root, read-only root FS; `ENV OTEL_SEMCONV_STABILITY_OPT_IN=http`; entrypoint `uvicorn identity.main:app --port 8000`. |
| `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_initial.py` | new | Alembic setup and the initial schema (§2.9). |
| `openapi.json` | new (generated) | Committed contract, written by `scripts/export_openapi.py identity`. |
| `src/identity/main.py` | new | `app = pycommon.create_app(settings, routers=[...])`. Lifespan: build the DB engine and Redis client, create the `PasswordHasher` (computes the dummy hash), start the email relay task (§6.9), the gauge refresher (§5.2) and the maintenance runner (§6.10); on shutdown, stop them in reverse order and wait for the in-flight send (≤ `IDENTITY_SMTP_TIMEOUT_S`). |
| `src/identity/settings.py` | new | `IdentitySettings(pycommon.BaseServiceSettings)`, env prefix `IDENTITY_`, with explicit `validation_alias` for the plan's unprefixed keys (§4). |
| `src/identity/startup_checks.py` | new | Identity-specific checks, registered with pycommon's startup checks (§4.3). |
| `src/identity/api/deps.py` | new | FastAPI dependencies: `public_caller` (requires `sub == "service:web"`), `user_caller` (requires a UUID `sub`), `admin_actor` (loads the actor row from the DB, requires `status = active` and `admin` in its DB roles), `client_ip` (parses `X-Client-IP`), `caller_rate_limit` (per `(iss, sub)` limit, fail open). |
| `src/identity/api/schemas.py` | new | All Pydantic request and response models (§3.3). |
| `src/identity/api/errors.py` | new | `IdentityError(code, status, message)` subclasses for every code in §3.5, raised by use cases and rendered by pycommon's handler. |
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
| `src/identity/domain/lockout.py` | new | Pure functions: `lock_seconds(level, base_s, max_s) -> int`; `LimitDecision` dataclass. No I/O. |
| `src/identity/domain/users.py` | new | Pure rules: `plan_user_update(actor_id, target, patch, other_active_admin_count) -> UserChange` raising `CannotModifySelf` / `LastAdmin`; `roles_changed(old, new) -> bool`. |
| `src/identity/domain/email_schedule.py` | new | `retry_delay_s(attempts, base_s, max_s) -> int`; `is_exhausted(attempts, max_attempts) -> bool`. |
| `src/identity/domain/ip.py` | new | `normalise_ip(raw: str) -> str` (via `ipaddress`; raises `ValueError`); `ip_hash(ip: str, key: bytes) -> str` (HMAC-SHA256 hex, 64 chars); `email_key(email: str, key: bytes) -> str` (HMAC-SHA256 hex of the lower-cased email, for Redis keys). |
| `src/identity/domain/audit_events.py` | new | Constants for every audit event name (§2.6). |
| `src/identity/db/models.py` | new | SQLAlchemy 2.0 declarative models, `MetaData(schema="identity")` (§2). |
| `src/identity/db/repositories.py` | new | `UserRepo`, `CredentialRepo`, `InviteRepo`, `ResetRepo`, `AuditRepo`, `OutboxRepo`. Every query takes `company_id` (from the token's `cid`) as a required argument. |
| `src/identity/db/session.py` | new | Engine and `async_sessionmaker` from pycommon; `statement_timeout` = `DB_STATEMENT_TIMEOUT_MS`. |
| `src/identity/limits.py` | new | Redis-backed login, reset and accept limiter with lockout: one Lua script per check (§6.4). Fails **closed**: raises `ServiceUnavailable` on any Redis error and logs `ratelimit.backend_error`. |
| `src/identity/email/templates.py` | new | `render(template, *, name, link, expires_at) -> RenderedEmail(subject, text, html)`; `link_for(template, raw_token, base_url) -> str` (§3.9). |
| `src/identity/email/templates/{invite,password_reset}.{txt,html}.j2` | new | Jinja2 templates; HTML with autoescape. |
| `src/identity/email/smtp.py` | new | `SmtpSender.send(msg: EmailMessage) -> None` over `aiosmtplib`, wrapped in pycommon's breaker (`target="smtp"`) and external-call metrics (`provider="smtp"`, `operation="send"`), timeout `IDENTITY_SMTP_TIMEOUT_S`. |
| `src/identity/email/relay.py` | new | `EmailRelay.run(stop: asyncio.Event)`: claim with `SKIP LOCKED`, issue the token, render, send, mark (§6.9). One instance per replica. |
| `src/identity/metrics.py` | new | Instruments (§5.2) and the `GaugeRefresher` task. |
| `src/identity/maintenance.py` | new | `async def run_maintenance(ctx) -> MaintenanceReport`, registered with pycommon's runner (advisory lock `identity.maintenance`, every `MAINTENANCE_INTERVAL_S`) (§6.10). |
| `src/identity/bootstrap_owner.py` | new | The CLI `python -m identity.bootstrap_owner` (§3.8). `main(argv) -> int`; `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`. |
| `tests/{unit,integration,contract}/` | new | §7. |

## 2. Data models

### 2.1 General

- Schema `identity`, owned by the migrator role (`IDENTITY_DB_MIGRATOR_USER`, default `identity_migrator`). The runtime role (`IDENTITY_DB_USER`, default `identity`) gets `SELECT, INSERT, UPDATE, DELETE` through the default privileges in the bootstrap SQL. The role names must match `deploy/compose/init/`.
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
| `expires_at` | `timestamptz` | no | | `created_at + INVITE_TTL_H`. |
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
| `expires_at` | `timestamptz` | no | | `created_at + RESET_TTL_H`. |
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

The migration runs `REVOKE UPDATE ON identity.audit_log FROM <runtime role>`, so the runtime role can append and purge but not rewrite entries.

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
- The counters are short-lived (≤ 24 h) and need atomic increment-with-TTL across replicas, which Redis does in one Lua call with no purge job.
- They sit on the hot login path; a Postgres row per attempt would add write load exactly when credential stuffing is happening.
- The fail-closed rule (plan §2.1, PAY-603) makes the Redis dependency safe: if Redis is down, login, reset and accept return 503 rather than skipping the limits.
- The cost: a Redis data loss (FLUSHALL, failover) resets counters and active locks. That loses at most one lock window of protection, which is acceptable; the audit log in Postgres keeps the history.

### 2.9 Alembic migration plan

- `alembic.ini` points at `migrations/`. `migrations/env.py`:
  - builds an async engine from the **migrator** DSN (`IDENTITY_DB_MIGRATOR_USER` / `IDENTITY_DB_MIGRATOR_PASSWORD`);
  - `context.configure(target_metadata=Base.metadata, version_table="alembic_version", version_table_schema="identity", include_schemas=False)`;
  - runs `SET search_path TO identity` and `SET lock_timeout = '5s'` before migrating.
- The schema itself is created by the bootstrap SQL (`deploy/compose/init/`), not by Alembic. The migration assumes it exists and is owned by the migrator.
- Revisions:
  - `0001_initial`: creates `users`, `credentials`, `invites`, `password_resets`, `audit_log`, `email_outbox`, with every constraint and index in §2.2–2.7, then `REVOKE UPDATE ON identity.audit_log FROM <runtime role>` (role name read from `IDENTITY_DB_USER` in `env.py`). Downgrade drops the tables in reverse FK order.
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
| `cid` | yes | Company UUID. Every query is scoped by it, never by input. |
| `roles` | yes | List of role strings; `[]` for `service:web`. identity re-reads roles from its DB for admin endpoints (§3.2). |
| `sid` | user tokens | Session hash. Not required for `service:web`. Never logged. |
| `iat`, `exp` | yes | `exp − iat` ≤ 60 s |
| `jti` | yes | UUID; not checked for replay (60 s lifetime). |

**Endpoints that accept `sub = service:web` with no user** (and only these): `POST /v1/auth/login`, `POST /v1/auth/password-reset/request`, `POST /v1/auth/password-reset/confirm`, `POST /v1/invites/accept`. They reject a user token, and every other endpoint rejects `service:web`, with 403 `forbidden_role`.

**Headers:**
- `Authorization: Bearer <jwt>`: all `/v1` routes.
- `X-Request-ID`: optional; echoed (pycommon).
- `X-Client-IP`: the browser's IP as the BFF derived it (`TRUSTED_PROXY_HOPS`). **Required** on the four public endpoints (missing or not a valid IPv4/IPv6 address → 422 `validation_error`). Optional elsewhere; when present it's hashed into `audit_log.ip_hash`. It's trusted only because the request carries a valid `iss=web` token. Never logged raw.

### 3.2 Authorization

| Dependency | Rule | Failure |
|---|---|---|
| `public_caller` | `sub == "service:web"` | 403 `forbidden_role` |
| `user_caller` | `sub` is a UUID | 403 `forbidden_role` |
| `admin_actor` | `user_caller`, then the actor row `(cid, sub)` exists with `status = 'active'` and `'admin' = ANY(roles)` **in the DB** (JWT roles may be up to `SESSION_ROLE_RECHECK_S` stale) | 403 `forbidden_role` |
| `caller_rate_limit` | User tokens: `SERVICE_RATE_LIMIT_USER` per minute per `(iss, sub)`, key `identity:rl:caller:web:<sub>`. Not applied to `service:web` (§9 issue 7). Fails open. | 429 `rate_limited` + `Retry-After` |

### 3.3 Pydantic models (`api/schemas.py`)

All models use `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`; trimming is explicit where stated. `Email = Annotated[EmailStr, StringConstraints(max_length=254)]`.

```python
class Role(StrEnum): admin = "admin"; payroll_admin = "payroll_admin"; approver = "approver"
Password = Annotated[str, StringConstraints(min_length=1, max_length=256)]   # policy (≥ 12) is checked in domain → 422 password_too_weak
Token    = Annotated[str, StringConstraints(min_length=1, max_length=128)]   # format is checked in domain → 400 invite_invalid / reset_invalid
Name     = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
RoleList = Annotated[list[Role], Field(min_length=1, max_length=3)]          # duplicates → 422 (validator)

class LoginIn(BaseModel):          email: Email; password: Password
class ResetRequestIn(BaseModel):   email: Email
class ResetConfirmIn(BaseModel):   token: Token; password: Password
class InviteAcceptIn(BaseModel):   token: Token; password: Password
class InviteUserIn(BaseModel):     email: Email; name: Name; roles: RoleList
class PatchUserIn(BaseModel):      roles: RoleList | None = None; status: Literal["inactive"] | None = None
                                   # model_validator: at least one field set, else 422

class SessionUser(BaseModel):      # returned by login and accept; the BFF builds the session from it
    user_id: UUID; company_id: UUID; email: str; name: str
    roles: list[Role]; roles_version: int; status: Literal["active"]

class PendingInviteOut(BaseModel):
    id: UUID; expires_at: datetime; expired: bool
    email_status: Literal["pending", "sent", "failed", "cancelled"] | None   # latest outbox row; None once purged

class UserOut(BaseModel):
    id: UUID; email: str; name: str; roles: list[Role]; roles_version: int
    status: Literal["pending", "active", "inactive"]
    created_at: datetime; updated_at: datetime; activated_at: datetime | None; deactivated_at: datetime | None
    pending_invite: PendingInviteOut | None          # set only when status == "pending"

class UserListOut(BaseModel):      items: list[UserOut]; next_cursor: str | None
class ResetConfirmOut(BaseModel):  user_id: UUID; roles_version: int   # the BFF deletes web:user-sess:<user_id>
class AuditEntryOut(BaseModel):
    id: UUID; at: datetime; actor_user_id: UUID | None; event: str
    target_user_id: UUID | None; ip_hash: str | None; details: dict[str, Any]
class AuditListOut(BaseModel):     items: list[AuditEntryOut]; next_cursor: str | None
```

Query parameters for lists: `limit: int = Query(50, ge=1, le=100)`, `cursor: str | None = Query(None, max_length=64)`. The cursor is the base64url (no padding) of the last row's UUID; a cursor that doesn't decode to a UUID returns 422 `validation_error`. Cursors never contain personal data.

### 3.4 Endpoints

Every endpoint can also return the **common errors**: 401 `invalid_token`; 413 `payload_too_large` (body > 256 KB, PAY-604); 422 `validation_error`; 500 `internal_error`; 503 `db_timeout` / `db_unavailable`. All responses carry `Cache-Control: no-store`. `operationId` is given for the generated TS client.

**POST /v1/auth/login** — `auth_login` — caller `service:web` — PAY-104
- Request `LoginIn`; header `X-Client-IP` required.
- 200 `SessionUser`.
- Errors: 401 `invalid_credentials` (wrong password, unknown email, user `pending` or `inactive`: same body, argon2 always runs); 429 `rate_limited` + `Retry-After` (IP limit, account limit, or account locked); 503 `service_unavailable` (Redis down: fail closed); 403 `forbidden_role` (user token).

**POST /v1/auth/password-reset/request** — `auth_password_reset_request` — caller `service:web` — PAY-105
- Request `ResetRequestIn`; header `X-Client-IP` required.
- 202, empty body, whether or not the email matches an active user.
- Errors: 429 `rate_limited` + `Retry-After`; 503 `service_unavailable`; 403 `forbidden_role`.

**POST /v1/auth/password-reset/confirm** — `auth_password_reset_confirm` — caller `service:web` — PAY-105
- Request `ResetConfirmIn`; header `X-Client-IP` required.
- 200 `ResetConfirmOut`.
- Errors: 400 `reset_invalid` (malformed, unknown, expired, used or revoked token; or the user is not `active`); 422 `password_too_weak`; 429 `rate_limited`; 503 `service_unavailable`; 403 `forbidden_role`.
- Precedence: the token is checked before the password policy.

**POST /v1/invites/accept** — `invites_accept` — caller `service:web` — PAY-103
- Request `InviteAcceptIn`; header `X-Client-IP` required.
- 200 `SessionUser` (the BFF then creates the session, PAY-106).
- Errors: 400 `invite_invalid` (malformed, unknown, expired, accepted or revoked token; or the user is not `pending`); 422 `password_too_weak`; 429 `rate_limited`; 503 `service_unavailable`; 403 `forbidden_role`.
- Precedence: token before password policy.

**GET /v1/users/me** — `users_me` — any user token — PAY-107.
- 200 `UserOut` for `(cid, sub)`, whatever its status (the BFF ends the session when `status != "active"` or `roles_version` differs).
- Errors: 404 `user_not_found`; 403 `forbidden_role` (`service:web`); 429 `rate_limited`.

**GET /v1/users** — `users_list` — admin — PAY-108
- Query `limit`, `cursor`. Ordered by `id` ascending (creation order).
- 200 `UserListOut`.
- Errors: 403 `forbidden_role`; 429 `rate_limited`.

**POST /v1/users/invite** — `users_invite` — admin — PAY-101
- Request `InviteUserIn`.
- 201 `UserOut` with `status = "pending"` and `pending_invite` set (`email_status = "pending"`).
- Errors: 409 `user_email_taken` (any existing user with that email in the company, any status, case-insensitive); 422 `validation_error` (empty or unknown role, duplicate roles, bad email or name); 403 `forbidden_role`; 429 `rate_limited`.

**POST /v1/invites/{id}/resend** — `invites_resend` — admin — PAY-102
- Path `id: UUID` (any invite of the user, including a revoked or expired one, so a stale UI id still works).
- 200 `PendingInviteOut` (the new invite).
- Errors: 404 `invite_not_found` (unknown id or another company); 409 `invite_not_pending` (the invite's user is not `pending`); 403 `forbidden_role`; 429 `rate_limited`.

**PATCH /v1/users/{id}** — `users_update` — admin, not on themselves — PAY-108
- Request `PatchUserIn`.
- 200 `UserOut` (unchanged and still 200 if the patch is a no-op).
- Errors: 404 `user_not_found`; 409 `cannot_modify_self` (`id == sub`); 409 `last_admin`; 422 `validation_error`; 403 `forbidden_role`; 429 `rate_limited`.

**GET /v1/audit-log** — `audit_log_list` — admin — PAY-109
- Query `limit`, `cursor`. Newest first (`id DESC`).
- 200 `AuditListOut`.
- Errors: 403 `forbidden_role`; 429 `rate_limited`.

**GET /livez**, **GET /readyz** — no JWT (pycommon) — PAY-605. `/readyz` checks Postgres, Redis and `alembic_version` = head.

There is no public sign-up route and no HTTP route for bootstrap (ADR 0002, PAY-100).

### 3.5 Error codes

| Code | Status | Raised by |
|---|---|---|
| `invalid_token` | 401 | pycommon JWT verifier (PAY-600) |
| `forbidden_role` | 403 | §3.2 (PAY-601) |
| `invalid_credentials` | 401 | login (PAY-104) |
| `invite_invalid` | 400 | accept (PAY-103) |
| `reset_invalid` | 400 | reset confirm (PAY-105) |
| `password_too_weak` | 422 | accept, reset confirm (PAY-103, PAY-105) |
| `user_email_taken` | 409 | invite (PAY-101) |
| `invite_not_pending` | 409 | resend (PAY-102) |
| `invite_not_found` | 404 | resend (new code; §9) |
| `user_not_found` | 404 | me, patch (new code; §9) |
| `cannot_modify_self` | 409 | patch (PAY-108) |
| `last_admin` | 409 | patch (PAY-108) |
| `rate_limited` | 429 + `Retry-After` | limiter (PAY-104, PAY-603) |
| `validation_error` | 422 | Pydantic, header and cursor validation; `details` = `[{field, code}]`, **never the input value** (passwords and tokens must not echo back) |
| `payload_too_large` | 413 | pycommon (PAY-604) |
| `service_unavailable` | 503 | Redis down on public flows (PAY-603) |
| `db_timeout` / `db_unavailable` | 503 | pycommon DB error mapping |
| `internal_error` | 500 | pycommon (PAY-602) |

### 3.6 Events

identity publishes and consumes no stream events. Email goes through its own `email_outbox` and in-process relay.

### 3.7 Redis keys

All under the ACL pattern `~identity:*` (PAY-607), user `identity`, password `REDIS_IDENTITY_PASSWORD`. Key list in §6.4.

### 3.8 `bootstrap_owner` CLI (PAY-100, PAY-609)

```
python -m identity.bootstrap_owner --email <email> --company-id <uuid> [--name <name>] [--password-env <VAR>]
```

| Argument | Required | Meaning |
|---|---|---|
| `--email` | yes | Owner email (validated like `InviteUserIn.email`). |
| `--company-id` | yes | UUID. Must equal `COMPANY_ID` when that's set, else exit 2. |
| `--name` | no (default `Owner`) | Display name, 1–200 characters. |
| `--password-env` | no | Name of an env var holding the owner's password (`SEED_OWNER_PASSWORD` in the Compose one-shot). **Allowed only when `ENV=dev`** (else exit 2). The password must pass the policy (else exit 2). The value is never logged or echoed. |

Behaviour, in one transaction on the **migrator** DSN:
1. `SELECT pg_advisory_xact_lock(hashtext('identity.bootstrap_owner'))` (two concurrent runs serialise).
2. If `SELECT 1 FROM identity.users LIMIT 1` returns a row (any company): log `identity.bootstrap.refused` (warning), exit 3.
3. Insert the user: roles `[admin, approver, payroll_admin]`, `roles_version = 1`, `created_by = NULL`.
   - Without `--password-env`: `status = 'pending'`, an invite (`created_by = NULL`, `expires_at = now() + INVITE_TTL_H`) and an `email_outbox` row (`template = 'invite'`). The running service's relay sends it.
   - With `--password-env` (dev only): `status = 'active'`, `activated_at = now()`, a credential row; no invite, no email.
4. Audit `user.bootstrapped` (`details.mode` = `invite` or `password`); log `identity.bootstrap.completed` with `user_id` and `mode`.

| Exit code | Meaning |
|---|---|
| 0 | Owner created |
| 1 | Unexpected error (logged with traceback, `identity.bootstrap.failed`) |
| 2 | Invalid arguments or configuration (argparse errors, bad email/UUID/name, `--company-id` ≠ `COMPANY_ID`, `--password-env` outside dev, the env var unset, or a weak password) |
| 3 | Refused: `identity.users` is not empty |
| 4 | Database unreachable or migrations not at head |

Exit 3 is the expected result on every re-run, so the Compose one-shot and the Kubernetes Job treat 0 and 3 as success.

### 3.9 Email templates

Rendered by `email/templates.py` with Jinja2 (`autoescape=True` for `.html.j2`, `StrictUndefined`). Each message is `multipart/alternative` (text + HTML) with `From: IDENTITY_EMAIL_FROM`, `To: <recipient>`, `Subject`, `Auto-Submitted: auto-generated`, and a generated `Message-ID`. No tracking pixels or external images.

**Link format** (token in the URL fragment, never in a query string; PAY-103, PAY-NFR-07):
- invite: `{IDENTITY_PUBLIC_BASE_URL}/accept#token={raw_token}`
- reset: `{IDENTITY_PUBLIC_BASE_URL}/reset#token={raw_token}`

`IDENTITY_PUBLIC_BASE_URL` has no trailing slash (validator strips it). `raw_token` is base64url without padding, so it needs no escaping.

`invite.txt.j2` (subject `You're invited to PayRun`):
```
Hello {{ name }},

You've been invited to PayRun. Open this link to set your password and activate your account:

{{ link }}

The link can be used once and expires at {{ expires_at_utc }} UTC.
If you weren't expecting this email, you can ignore it.
```

`password_reset.txt.j2` (subject `Reset your PayRun password`):
```
Hello {{ name }},

Someone asked to reset your PayRun password. Open this link to choose a new one:

{{ link }}

The link can be used once and expires at {{ expires_at_utc }} UTC.
If you didn't ask for this, ignore this email; your password won't change.
```

The `.html.j2` versions carry the same text, with the link as an `<a href>` and as visible text. `expires_at_utc` is `expires_at` formatted `YYYY-MM-DD HH:MM`.

## 4. Configuration

### 4.1 Naming

- Keys the plan or requirements already name keep their exact names, without the `IDENTITY_` prefix (for example `INVITE_TTL_H`, `AUTH_LOGIN_LIMIT_ACCOUNT`, `SMTP_HOST`). `settings.py` reads them with `validation_alias`.
- Identity-only keys this design introduces use the `IDENTITY_` prefix.
- "gen" means generated by `scripts/dev-env.sh`; "ES" means External Secrets.

### 4.2 Keys

| Key | Type | dev | uat | prod | Requirement |
|---|---|---|---|---|---|
| `ENV` | `Literal["dev","uat","prod"]` | `dev` | `uat` | `prod` | PAY-605 |
| `COMPANY_ID` | UUID | fixed dev UUID | per env | per env | PAY-100 |
| `IDENTITY_DB_HOST` / `IDENTITY_DB_PORT` / `IDENTITY_DB_NAME` | str / int / str | `postgres` / 5432 / `payrun` | managed host | managed host | PAY-606 |
| `IDENTITY_DB_USER` / `IDENTITY_DB_MIGRATOR_USER` | str | `identity` / `identity_migrator` | same | same | PAY-606 |
| `IDENTITY_DB_PASSWORD` / `IDENTITY_DB_MIGRATOR_PASSWORD` | SecretStr | gen | ES | ES | PAY-606 |
| `DB_STATEMENT_TIMEOUT_MS` / `DB_POOL_TIMEOUT_S` | int | 2000 / 2 | same | same | PAY-605 |
| `IDENTITY_DB_POOL_SIZE` | int | 10 | 10 | 10 | PAY-NFR-05 |
| `IDENTITY_REDIS_URL` | str (no password) | `redis://identity@redis:6379/0` | managed | managed | PAY-607 |
| `REDIS_IDENTITY_PASSWORD` | SecretStr | gen | ES | ES | PAY-607 |
| `REDIS_SOCKET_TIMEOUT_MS` | int | 500 | 500 | 500 | PAY-603 |
| `INTERNAL_JWT_PUBLIC_KEYS` | JSON `{kid: {iss, pem}}` | gen (web key only) | ConfigMap | ConfigMap | PAY-600 |
| `AUDIT_IP_HASH_KEY` | SecretStr (≥ 32 bytes) | gen | ES | ES | PAY-NFR-08 |
| `INVITE_TTL_H` | int | 72 | 72 | 72 | PAY-101 |
| `RESET_TTL_H` | int | 1 | 1 | 1 | PAY-105 |
| `AUTH_LOGIN_LIMIT_ACCOUNT` | int (attempts per window) | 5 (raised in `compose.test.yaml`) | 5 | 5 | PAY-104 |
| `AUTH_LOGIN_LIMIT_IP` | int (attempts per window) | 20 (raised in `compose.test.yaml`) | 20 | 20 | PAY-104 |
| `IDENTITY_LOGIN_WINDOW_S` | int | 900 | 900 | 900 | PAY-104 |
| `IDENTITY_LOCKOUT_BASE_S` / `IDENTITY_LOCKOUT_MAX_S` | int | 60 / 3600 | same | same | PAY-104 |
| `IDENTITY_LOCKOUT_LEVEL_TTL_S` | int | 86400 | 86400 | 86400 | PAY-104 |
| `SERVICE_RATE_LIMIT_USER` | int per min | 600 (raised in test) | 600 | 600 | PAY-603 |
| `IDENTITY_ARGON2_TIME_COST` / `_MEMORY_KIB` / `_PARALLELISM` | int | 3 / 65536 / 4 | same | same | PAY-103, PAY-104 |
| `IDENTITY_ARGON2_MAX_CONCURRENCY` | int | 4 | 4 | 4 | PAY-NFR-05 |
| `IDENTITY_PUBLIC_BASE_URL` | URL | `http://localhost:4100` | `https://<uat host>` | `https://<prod host>` | PAY-103, PAY-105 |
| `IDENTITY_EMAIL_FROM` | str | `PayRun <no-reply@payrun.local>` | per env | per env | PAY-110 |
| `SMTP_HOST` | str | `mailpit` | `mailpit` (in-cluster) | ES | PAY-110 |
| `IDENTITY_SMTP_PORT` | int | 1025 | 1025 | 587 | PAY-110 |
| `IDENTITY_SMTP_STARTTLS` | bool | false | false | true | PAY-110 |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | str / SecretStr | empty | empty | ES | PAY-110 |
| `IDENTITY_SMTP_TIMEOUT_S` | float | 5 | 5 | 5 | PAY-110 |
| `EMAIL_MAX_ATTEMPTS` | int | 10 | 10 | 10 | PAY-110 |
| `IDENTITY_EMAIL_RETRY_BASE_S` / `IDENTITY_EMAIL_RETRY_MAX_S` | int | 30 / 1800 | same | same | PAY-110 |
| `IDENTITY_EMAIL_POLL_INTERVAL_S` | float | 2 | 2 | 2 | PAY-110 |
| `IDENTITY_EMAIL_BATCH` | int | 10 | 10 | 10 | PAY-110 |
| `IDENTITY_EMAIL_LEASE_S` | int | 60 | 60 | 60 | PAY-110 |
| `BREAKER_FAILURES` / `BREAKER_RESET_S` | int | 5 / 30 | same | same | PAY-110 |
| `MAINTENANCE_INTERVAL_S` | int | 60 | 60 | 60 | PAY-608 |
| `RETENTION_TOKENS_DAYS` | int | 7 | 7 | 7 | PAY-608 |
| `RETENTION_OUTBOX_DAYS` | int (used for sent/failed/cancelled emails) | 7 | 7 | 7 | PAY-608 |
| `IDENTITY_RETENTION_AUDIT_DAYS` | int | 400 | 400 | 400 | PAY-NFR-08 |
| `IDENTITY_GAUGE_REFRESH_S` | int | 15 | 15 | 15 | PAY-NFR-10 |
| `IDENTITY_DOCS_ENABLED` | bool | true | false | false | PAY-605 |
| `LOG_LEVEL` | str | `debug` | `info` | `info` | PAY-NFR-07 |
| `OTEL_SERVICE_NAME` | str | `identity` | same | same | PAY-NFR-09 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL | `http://otel-collector:4317` | per env | per env | PAY-NFR-09 |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | str (Dockerfile `ENV`) | `http` | `http` | `http` | PAY-NFR-10 |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | str / float | `parentbased_traceidratio` / 1.0 | 1.0 | 0.1 | PAY-NFR-09 |

Pod sizing note: `IDENTITY_ARGON2_MAX_CONCURRENCY × IDENTITY_ARGON2_MEMORY_KIB` = 256 MiB of hashing memory at peak, so the identity container's memory limit must be at least 512 Mi.

### 4.3 Identity startup checks (uat and prod; PAY-605)

In addition to pycommon's checks (missing or dev-default secrets, `dev-`/`test-` kids, debug, docs):
- every kid in `INTERNAL_JWT_PUBLIC_KEYS` is bound to `iss = "web"`;
- `AUDIT_IP_HASH_KEY` is set, ≥ 32 bytes, and doesn't start with `dev-` or `test-`;
- `IDENTITY_PUBLIC_BASE_URL` starts with `https://`;
- prod only: `IDENTITY_SMTP_STARTTLS` is true and `SMTP_USERNAME` / `SMTP_PASSWORD` are set;
- `AUTH_LOGIN_LIMIT_ACCOUNT` ≤ 5, `AUTH_LOGIN_LIMIT_IP` ≤ 20 and `IDENTITY_LOCKOUT_BASE_S` ≥ 60 (the raised test values can't reach uat or prod);
- `IDENTITY_DOCS_ENABLED` is false.
A failing check logs `startup.check_failed` with the setting's name only and exits non-zero.

## 5. Logging and metrics

### 5.1 Log events

Every line has pycommon's base fields (`ts`, `level`, `service`, `env`, `version`, `logger`, `event`, `msg`, `trace_id`, `span_id`, `request_id`). Identity never logs emails, names, passwords, tokens, token hashes, raw IPs, `sid` or JWTs; it logs IDs, `ip_hash` and counts.

| Event | Level | Fields | When | Requirement |
|---|---|---|---|---|
| `identity.bootstrap.completed` | info | `user_id`, `company_id`, `mode` | CLI created the owner | PAY-100 |
| `identity.bootstrap.refused` | warning | `company_id` | `users` not empty (exit 3) | PAY-100 |
| `identity.bootstrap.failed` | error | `reason` (`invalid_args`, `db_unavailable`, `unexpected`) | Exit 1, 2 or 4 | PAY-100 |
| `user.invited` | info | `user_id`, `invite_id`, `actor_user_id`, `roles` | Invite created | PAY-101 |
| `user.invite_resent` | info | `user_id`, `invite_id`, `revoked_invite_id`, `actor_user_id` | Resend | PAY-102 |
| `user.invite_accepted` | info | `user_id`, `invite_id` | Accept succeeded | PAY-103 |
| `invite.invalid` | info | `reason` (`malformed`, `unknown`, `expired`, `not_pending`, `user_not_pending`) | Accept rejected | PAY-103 |
| `auth.signin.succeeded` | info | `user_id`, `ip_hash`, `rehashed` (bool) | Login 200 | PAY-104 |
| `auth.signin.failed` | info | `user_id` (or absent), `reason` (`bad_password`, `unknown_user`, `not_active`), `ip_hash` | Login 401 | PAY-104 |
| `auth.signin.rate_limited` | warning | `scope` (`ip`, `account`), `ip_hash`, `retry_after_s` | Login 429 | PAY-104 |
| `auth.lockout` | warning | `user_id` (or absent), `level`, `lock_seconds`, `ip_hash` | An account lock starts | PAY-104 |
| `auth.password_reset.requested` | info | `matched` (bool), `user_id` (if matched), `reset_id` (if matched), `ip_hash` | Reset request 202 | PAY-105 |
| `auth.password_reset.completed` | info | `user_id`, `reset_id`, `roles_version` | Confirm 200 | PAY-105 |
| `auth.password_reset.invalid` | info | `reason` (as `invite.invalid`) | Confirm 400 | PAY-105 |
| `user.roles_changed` | info | `user_id`, `actor_user_id`, `old_roles`, `new_roles`, `roles_version` | PATCH roles | PAY-108 |
| `user.deactivated` | info | `user_id`, `actor_user_id`, `roles_version`, `revoked_invite_id` (if any) | PATCH status | PAY-108 |
| `user.update_refused` | info | `user_id`, `actor_user_id`, `code` (`cannot_modify_self`, `last_admin`) | PATCH 409 | PAY-108 |
| `email.queued` | info | `email_id`, `template`, `user_id` | Outbox row inserted | PAY-110 |
| `email.sent` | info | `email_id`, `template`, `attempts`, `duration_ms` | SMTP accepted | PAY-110 |
| `email.send_failed` | warning | `email_id`, `template`, `attempts`, `error` (class/code), `next_attempt_in_s` | Attempt failed, will retry | PAY-110 |
| `email.failed` | error | `email_id`, `template`, `attempts`, `error` | `EMAIL_MAX_ATTEMPTS` reached | PAY-110 |
| `email.cancelled` | info | `email_id`, `template`, `reason` (`invite_not_pending`, `reset_not_pending`, `expired`) | Token no longer issuable | PAY-110 |
| `email.relay.error` | error | `error` | Unexpected error in the relay loop (it continues after 5 s) | PAY-110 |
| `ratelimit.backend_error` | warning | `component` (`login`, `reset`, `accept`, `caller`), `error` | Redis error in a limiter | PAY-603 |
| `maintenance.purged` | info | `invites_deleted`, `invite_hashes_nulled`, `resets_deleted`, `emails_deleted`, `audit_deleted`, `duration_ms` | Each maintenance run | PAY-608 |
| `auth.token.rejected` | warning | `reason` (pycommon) | JWT 401 | PAY-600 |
| `http.request` | info | pycommon (route template, status, duration_ms) | Every request | PAY-NFR-09 |
| `startup.check_failed` | error | `setting` | Startup check | PAY-605 |

### 5.2 Metrics

Names as in plan §11 and `observability.md` §5; labels bounded.

| Metric | Type | Labels | When | Requirement |
|---|---|---|---|---|
| `auth_signins_total` | counter | `outcome`: `success`, `failure`, `rate_limited` | Every login response (401 → `failure`, 429 → `rate_limited`) | PAY-104, PAY-NFR-10 |
| `rate_limited_total` | counter | `scope`: `ip`, `account`, `caller` | Every 429 from identity (login, reset, accept, per-caller) | PAY-603 |
| `rate_limiter_errors_total` | counter | `component`: `login`, `reset`, `accept`, `caller` | Every limiter Redis error | PAY-603 |
| `identity_emails_pending` | gauge | — | Count of `status = 'pending'` | PAY-110 |
| `identity_emails_failed` | gauge | — | Count of `status = 'failed'` (until purged) | PAY-110 |
| `identity_email_oldest_pending_age_seconds` | gauge | — | `now − min(created_at)` of pending rows; 0 when none | PAY-110 |
| `external_requests_total` | counter | `provider="smtp"`, `operation="send"`, `outcome`: `ok`, `error`, `timeout`, `breaker_open` | Every SMTP send attempt | PAY-110 |
| `external_request_duration_seconds` | histogram | `provider="smtp"`, `operation="send"` | Every SMTP send | PAY-110 |
| `circuit_breaker_state` | gauge | `target="smtp"` | Breaker transitions (pycommon) | PAY-110 |
| `identity_password_hash_seconds` | histogram | `op`: `hash`, `verify` | Each argon2 call (sizing and the timing-class check) | PAY-104 |
| `http_server_request_duration_seconds` | histogram | auto (route, status) | Every request | PAY-NFR-05 |

The three email gauges are computed by **every replica** from one DB query every `IDENTITY_GAUGE_REFRESH_S` (a background task outside the advisory lock); the OTel observable-gauge callback reads the cached values. Alert rules use `max()` across replicas (like plan §2.1's payroll gauges).

### 5.3 Spans worth adding manually

FastAPI, SQLAlchemy, Redis and httpx spans come from auto-instrumentation. Manual spans (`tracer = trace.get_tracer("identity")`), with attributes limited to IDs, outcomes and counts:

| Span | Attributes | Why |
|---|---|---|
| `identity.login` | `identity.outcome`, `identity.user_id` (when known) | One place to see a sign-in's limiter, lookup and verify |
| `identity.password.verify` / `identity.password.hash` | `identity.dummy` (bool) | Argon2 is the dominant cost; shows semaphore wait |
| `identity.ratelimit.check` | `identity.component`, `identity.allowed` | Redis limiter latency |
| `identity.invite.create` / `.resend` / `.accept` | `identity.user_id`, `identity.invite_id` | J1 steps |
| `identity.reset.request` / `.confirm` | `identity.user_id` (when matched) | |
| `identity.user.update` | `identity.user_id`, `identity.roles_changed`, `identity.deactivated` | |
| `identity.email.claim` | `identity.batch_size` | Relay throughput |
| `identity.email.send` | `identity.email_id`, `identity.template`, `identity.attempt` | Started as a **child of the outbox row's `traceparent`**, so the invite request and its email are one trace |
| `identity.maintenance.run` | counts per purge | |

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
- Reset request does the same single user lookup in both cases and always returns 202 with an empty body. The extra inserts for a match take a few milliseconds, well inside the timing class.

### 6.4 Rate limits and lockout (PAY-104, PAY-105, PAY-603)

`ek = email_key(lower(email))`, `ih = ip_hash(ip)`; both HMAC-SHA256 with `AUDIT_IP_HASH_KEY`, so Redis holds no emails or raw IPs.

| Key | Type | TTL | Meaning |
|---|---|---|---|
| `identity:rl:login:ip:<ih>` | counter | `IDENTITY_LOGIN_WINDOW_S` from the first hit | Login attempts from this IP |
| `identity:rl:login:acct:<ek>` | counter | `IDENTITY_LOGIN_WINDOW_S` from the first hit | Login attempts for this email since the last success or lock |
| `identity:rl:login:lock:<ek>` | string `1` | the lock duration | Account locked while present |
| `identity:rl:login:level:<ek>` | counter | `IDENTITY_LOCKOUT_LEVEL_TTL_S`, refreshed on each lock | Number of locks in the last 24 h |
| `identity:rl:reset:ip:<ih>` / `identity:rl:reset:acct:<ek>` | counter | window | Reset requests (same limits as login, no lock) |
| `identity:rl:accept:ip:<ih>` | counter | window | Invite-accept and reset-confirm attempts, limit `AUTH_LOGIN_LIMIT_IP` |
| `identity:rl:caller:web:<sub>` | pycommon limiter | 60 s | Per-user service limit (`SERVICE_RATE_LIMIT_USER`) |

**Login algorithm** (steps 1–3 in one Lua script, so concurrent attempts can't overshoot):
1. `INCR login:ip:<ih>` (set `EXPIRE` when the result is 1). If > `AUTH_LOGIN_LIMIT_IP` → 429, `Retry-After = TTL`, `scope=ip`.
2. If `login:lock:<ek>` exists → 429, `Retry-After = ceil(PTTL / 1000)`, `scope=account`. No argon2 runs; the attempt doesn't extend the lock or count toward the next level.
3. `INCR login:acct:<ek>` (set `EXPIRE` when 1). If > `AUTH_LOGIN_LIMIT_ACCOUNT`:
   - `level = INCR login:level:<ek>`; `EXPIRE login:level:<ek> IDENTITY_LOCKOUT_LEVEL_TTL_S`;
   - `lock_s = lock_seconds(level) = min(IDENTITY_LOCKOUT_BASE_S × 2^(level−1), IDENTITY_LOCKOUT_MAX_S)` (60, 120, 240, 480, 960, 1920, 3600, 3600 … s);
   - `SET login:lock:<ek> 1 EX lock_s`; `DEL login:acct:<ek>`;
   - → 429, `Retry-After = lock_s`; audit and log `auth.lockout`.
4. Otherwise verify the password (§6.3).
5. Success: `DEL login:acct:<ek> login:level:<ek>` → 200. Failure: → 401 (the attempt was already counted in step 3).

With the production values, attempts 1–5 in 15 minutes return 401, the 6th returns 429 and locks for 60 s; after each lock the next 5 failures lock for twice as long, up to 60 minutes. A lock level decays after 24 h without a new lock.

**Reset request:** steps 1 and 3 with the `reset:*` keys and the same limits; no lock. **Accept and reset confirm:** step 1 with `accept:ip:<ih>` only.

**Redis errors:** the four public flows fail **closed** (503 `service_unavailable`); the per-caller limit fails **open**. Both increment `rate_limiter_errors_total{component}` and log `ratelimit.backend_error`.

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
6. Apply: roles (sorted, de-duplicated), `status = 'inactive'` + `deactivated_at = now()`; bump `roles_version`; on deactivation also set the user's pending invite (if any) to `revoked` and cancel its pending outbox row; write `user.roles_changed` and/or `user.deactivated`.

Only `inactive` is accepted for `status` in 0.1.0 (§9 issue 9).

### 6.6 Invite, resend and accept (PAY-101–103)

- **Invite** (one transaction): insert `users` (pending), `invites` (`token_hash = NULL`, `expires_at = now() + INVITE_TTL_H`), `email_outbox` (template `invite`, current `traceparent`), audit `user.invited`. A unique violation on `uq_users_company_email` → 409 `user_email_taken`.
- **Resend** (one transaction): load the invite by `(cid, id)` (404 `invite_not_found`); lock its user; if the user isn't `pending` → 409 `invite_not_pending`. Set the user's pending invite (if any) to `revoked`, cancel that invite's pending outbox rows, insert a new invite and outbox row, audit `user.invite_resent`. The old token then fails with `invite_invalid` (PAY-102).
- **Accept:**
  1. `digest = digest_of(token)`; malformed → 400 `invite_invalid`.
  2. `SELECT` the invite by `token_hash` and `company_id`; it must be `pending`, `expires_at > now()`, and its user `pending`. Else 400 `invite_invalid`.
  3. Password policy → 422 `password_too_weak`.
  4. Hash the password (outside the transaction).
  5. One transaction: `UPDATE invites SET status = 'accepted', accepted_at = now() WHERE id = :id AND status = 'pending' AND expires_at > now() RETURNING user_id` (0 rows → 400 `invite_invalid`, which handles a concurrent double accept); `UPDATE users SET status = 'active', activated_at = now() WHERE id = :uid AND status = 'pending'` (0 rows → rollback, 400); insert `credentials`; audit `user.invite_accepted`.
  6. Return `SessionUser`.
- **Reset request:** if an active user matches: revoke their pending reset, insert `password_resets` (`expires_at = now() + RESET_TTL_H`) and an outbox row (`password_reset`), audit `auth.password_reset.requested`. Always 202.
- **Reset confirm:** like accept (400 `reset_invalid`; user must be `active`); then in one transaction mark the reset `used`, upsert `credentials` (`password_changed_at = now()`), bump `roles_version`, audit `auth.password_reset.completed`. Return `{user_id, roles_version}`. The BFF deletes the `web:user-sess:<user_id>` sessions (identity's Redis ACL can't reach `web:*`); the `roles_version` bump makes any session it misses end at the next re-check (PAY-107).

### 6.7 Token generation and hashing

- `new_token()`: `raw_bytes = secrets.token_bytes(32)`; `raw = base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode()` (43 characters); `digest = hashlib.sha256(raw_bytes).digest()` (32 bytes).
- `digest_of(raw)`: returns `None` unless `raw` matches `^[A-Za-z0-9_-]{43}$` and decodes to 32 bytes; otherwise the SHA-256 of the decoded bytes.
- Only the digest is stored (`token_hash`). Lookup is by the unique digest index, so no secret-dependent comparison happens in Python.
- **The token is issued by the relay at send time**, not when the invite or reset row is created. The raw token exists only in the relay's memory while rendering and sending one message. That's how the outbox stores a token *reference* and never a body or a token (§9 issue 3).
  - Each send attempt that reaches SMTP issues a fresh token (`UPDATE invites SET token_hash = :d, token_issued_at = now() WHERE id = :id AND status = 'pending' AND expires_at > now()`), so only the most recently sent link works. If an email was delivered but the row wasn't marked `sent` (crash), the redelivered email carries the working link.
  - Expiry runs from row creation (`INVITE_TTL_H`, `RESET_TTL_H`), not from sending.
- Single use: accept and confirm move the row out of `pending` with a conditional `UPDATE`.

### 6.8 IP hashing

`ip_hash = hmac.new(AUDIT_IP_HASH_KEY, normalise_ip(ip).encode(), sha256).hexdigest()`, where `normalise_ip` uses `ipaddress.ip_address(...).compressed` (IPv4-mapped IPv6 converted to IPv4). Raw IPs are never stored or logged (PAY-NFR-08).

### 6.9 Email relay loop (PAY-110)

One `EmailRelay` task per replica; safe with several replicas.

```
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
  for each claimed row, in order:
    1. issue the token (own transaction, §6.7). No row updated → status 'cancelled', finished_at = now(); log email.cancelled; next.
    2. render (§3.9) with the user's name and expires_at.
    3. send through SmtpSender (breaker, timeout IDENTITY_SMTP_TIMEOUT_S), in span identity.email.send (parent = row.traceparent).
    4. ok        → status 'sent', finished_at = now(), last_error NULL; log email.sent.
       breaker open → attempts = attempts − 1, next_attempt_at = now() + BREAKER_RESET_S (not counted).
       error     → if attempts >= EMAIL_MAX_ATTEMPTS: status 'failed', finished_at = now(); log email.failed
                   else next_attempt_at = now() + retry_delay_s(attempts); log email.send_failed.
                   last_error = error class or "smtp_<code>".
  if fewer than IDENTITY_EMAIL_BATCH rows were claimed: wait IDENTITY_EMAIL_POLL_INTERVAL_S (or until stop).
```

- **Backoff:** `retry_delay_s(n) = min(IDENTITY_EMAIL_RETRY_BASE_S × 2^(n−1), IDENTITY_EMAIL_RETRY_MAX_S)`, with ±10% jitter. After failures 1–9: 30, 60, 120, 240, 480, 960, 1800, 1800, 1800 s (about 2 h in total); the 10th failure marks the row `failed`.
- **Lease:** a crash after the claim leaves the row `pending` with `next_attempt_at` 60 s ahead, so another replica retries it then; the crashed attempt counts toward `EMAIL_MAX_ATTEMPTS`, which bounds poison rows.
- **Never stored:** the rendered subject and body, the link and the raw token are local variables only. `aiosmtplib` debug logging is off; the redactor masks `token`, `link` is never a log field.
- **Shutdown:** the loop stops claiming when `stop` is set, finishes the message in flight, and returns. Claimed but unsent rows are retried after their lease.
- **Unexpected errors** in the loop are logged (`email.relay.error`), and the loop sleeps 5 s and continues.

### 6.10 Maintenance (PAY-608)

pycommon's runner calls `run_maintenance` every `MAINTENANCE_INTERVAL_S` under `pg_try_advisory_lock(hashtext('identity.maintenance'))` (one replica at a time). Each delete runs in batches of 1000 until done.
- Invites: `UPDATE invites SET token_hash = NULL WHERE expires_at < now() − RETENTION_TOKENS_DAYS AND token_hash IS NOT NULL`; `DELETE FROM invites WHERE status IN ('accepted','revoked') AND expires_at < now() − RETENTION_TOKENS_DAYS`. A pending user's latest expired invite is kept (hash nulled), so an admin can still resend it.
- Password resets: `DELETE WHERE expires_at < now() − RETENTION_TOKENS_DAYS`.
- Emails: `DELETE FROM email_outbox WHERE status IN ('sent','failed','cancelled') AND finished_at < now() − RETENTION_OUTBOX_DAYS`.
- Audit: `DELETE FROM audit_log WHERE at < now() − IDENTITY_RETENTION_AUDIT_DAYS`.
- Logs `maintenance.purged` with the counts.

Failing emails after `EMAIL_MAX_ATTEMPTS` happens in the relay (§6.9), not here.

## 7. Tests

Every test names its requirement IDs in its docstring. Unit tests run with `--disable-socket --allow-unix-socket`. Integration tests use the shared Testcontainers fixtures (Postgres 16 with the bootstrap SQL, Redis 7 with the ACL file, and a Mailpit container), migrate as the migrator, and connect as the runtime role.

### 7.1 `services/identity/tests/unit/`

| Test file | Covers | Requirement |
|---|---|---|
| `test_passwords.py` | Policy (11 → `PasswordTooWeak`, 12 and 256 OK, code points not bytes); argon2 parameters read from the encoded hash (`m=65536,t=3,p=4`, argon2id); `verify` with `None` uses the dummy hash and returns false; `needs_rehash` after a parameter change | PAY-103, PAY-104 |
| `test_tokens.py` | 32 random bytes, 43-char base64url, digest = SHA-256 of the raw bytes, `digest_of` rejects wrong length, padding and alphabet | PAY-101, PAY-103, PAY-105 |
| `test_lockout.py` | `lock_seconds` sequence 60, 120 … 3600 and the cap; settings defaults equal the production values (5, 20, 900, 60, 3600) | PAY-104 |
| `test_user_rules.py` | `cannot_modify_self`; `last_admin` for demote and for deactivate; pending admins don't count; `roles_version` bumps only on a real change or deactivation; role de-duplication and ordering | PAY-108, PAY-107 |
| `test_email_schedule.py` | Backoff sequence and cap; exhaustion at `EMAIL_MAX_ATTEMPTS` | PAY-110 |
| `test_email_templates.py` | Links are `{base}/accept#token=` and `{base}/reset#token=`; no `?` in the link; base URL trailing slash stripped; HTML escapes the name; subject has no token | PAY-103, PAY-105, PAY-NFR-07 |
| `test_ip.py` | IPv4/IPv6 normalisation; `ip_hash` is HMAC-SHA256 with the key (known vector), 64 hex chars, differs with another key | PAY-NFR-08 |
| `test_routes_auth.py` | Parametrised matrix: every route × {no token, wrong `aud`, `iss` not web, `service:web`, user without admin, admin} → 401 / 403 / 2xx as in §3.2; `service:web` accepted only on the four public routes | PAY-600, PAY-601 |
| `test_api_errors.py` | Each error code in §3.5 maps to its status and shape; `validation_error` details never contain the input (a 300-char password, a token) | PAY-602, PAY-NFR-07 |
| `test_redaction.py` | **The redaction test (unit):** with an in-memory log handler and span exporter, call login, accept, reset confirm and the relay's send with fakes; assert that no log line or span attribute contains the password, the raw token, the email, the raw IP or the `Authorization` header, and that every identity log event's fields are in §5.1's allow-list | PAY-NFR-07 |
| `test_metrics.py` | `InMemoryMetricReader`: a 401 increments `auth_signins_total{outcome="failure"}`, a 429 increments `rate_limited_total{scope="account"}` and `auth_signins_total{outcome="rate_limited"}`, an SMTP error increments `external_requests_total{provider="smtp",outcome="error"}`, a limiter error increments `rate_limiter_errors_total{component="login"}` | PAY-NFR-10, PAY-603 |
| `test_startup_checks.py` | Each §4.3 check fails in uat/prod with the setting's name and no value; passes in dev | PAY-605 |
| `test_bootstrap_args.py` | Argument parsing and exit code 2 cases (bad email, bad UUID, `--password-env` outside dev, unset env var, weak password, company id ≠ `COMPANY_ID`) | PAY-100, PAY-609 |

### 7.2 `services/identity/tests/integration/`

| Test file | Covers | Requirement |
|---|---|---|
| `test_migrations.py` | Upgrade to head, downgrade one step, upgrade again; `alembic_version` lives in schema `identity`; all §2 constraints exist | PAY-606 |
| `test_grants.py` | Runtime role: `permission denied` on `payroll.*`, `payouts.*`, `bank_stub.*` and on `CREATE TABLE identity.x`; `UPDATE identity.audit_log` denied; DML on the other identity tables allowed. Migrator role: no rights in other schemas | PAY-606 |
| `test_redis_acl.py` | The `identity` Redis user can use `identity:*` but gets `NOPERM` on `web:sess:x` and on `XADD stream:payout.requested` | PAY-607 |
| `test_bootstrap_owner.py` | Empty DB → exit 0, pending owner with all three roles, invite and outbox row, audit `user.bootstrapped`; second run → exit 3 and `identity.bootstrap.refused`; two concurrent runs → one 0, one 3; `--password-env` in dev → active owner who can log in; DB down → exit 4 | PAY-100, PAY-609 |
| `test_invites.py` | Invite 201 with pending user and `email_status="pending"`; 409 `user_email_taken` (case-insensitive, concurrent duplicates); 422 for empty/unknown roles; 403 for non-admin; resend 200 revokes the old token (`invite_invalid` afterwards) and 409 `invite_not_pending` for an active user; accept: 200 `SessionUser`, used / expired / unknown / revoked token → 400, 11-char password → 422, two concurrent accepts → one 200; the token travels only in the body | PAY-101, PAY-102, PAY-103 |
| `test_login.py` | 200 for an active user; identical 401 body for wrong password, unknown email, pending and inactive users; argon2 runs on every path (`identity_password_hash_seconds{op="verify"}` count increments for each); audit rows with `ip_hash` and no raw IP; rehash on parameter change | PAY-104, PAY-NFR-08 |
| `test_lockout.py::test_lockout_production_values` | With settings forced to the production values (5 / 20 / 900 s / 60 s / 3600 s) regardless of the test env: attempts 1–5 → 401, the 6th → 429 with `Retry-After` 60 and `auth.lockout` audited; the correct password during the lock → 429; after the lock key is expired (`PEXPIRE` 1 ms), 5 more failures lock with TTL 120; success clears the counters and level; unknown emails lock identically; 21 attempts from one IP over different emails → the 21st is 429 `scope=ip` | PAY-104 |
| `test_password_reset.py` | Request is 202 for known, unknown and inactive emails, with an outbox row only for the active user; confirm sets the password (old fails, new works), bumps `roles_version`, returns `user_id`; reused / expired / unknown token → 400 `reset_invalid`; reset requests rate-limited like login | PAY-105 |
| `test_users_admin.py` | List with cursor and limit ≤ 100 (101 → 422); `/me` returns status and `roles_version`; PATCH roles bumps `roles_version` and audits `user.roles_changed`; deactivate bumps, audits `user.deactivated` and revokes a pending invite; self-PATCH → 409 `cannot_modify_self`; last-admin guard (two admins demoting each other concurrently: one 200, one 403/409, never zero admins); actor demoted in DB but JWT still says admin → 403 | PAY-107, PAY-108, PAY-601 |
| `test_audit_log.py` | Newest first, cursor paging, admin only; after a full J1 run (invite, accept, login fail and success, reset, role change) the table contains the expected events and **no** password, token, email or raw IP in any column | PAY-109, PAY-NFR-08 |
| `test_email_relay.py` | Invite → Mailpit receives one message with a `/accept#token=` link whose token accepts the invite; SMTP stopped → invite still 201, row stays pending with growing `attempts`, sent after Mailpit restarts; 10 failures → `failed` and `identity_emails_failed` = 1; two relays on one table never send a row twice (SKIP LOCKED); a resent invite's old outbox row is `cancelled`; no column of `email_outbox` contains the token or the link | PAY-110 |
| `test_rate_limits.py` | Two app instances share login counts; Redis stopped → login, reset request, reset confirm and accept return 503 `service_unavailable`, while `/v1/users/me` still works (fail open) and `rate_limiter_errors_total` increments; per-caller limit 429 with `Retry-After` | PAY-603 |
| `test_maintenance.py` | Expired tokens purged after `RETENTION_TOKENS_DAYS` (time-machine), a pending user's latest invite kept with hash nulled; sent/failed/cancelled emails purged after 7 days; audit rows past 400 days purged; only one replica runs under the advisory lock | PAY-608, PAY-NFR-08 |
| `test_readyz.py` | `/readyz` fails with the DB down, with Redis down, and when the migration isn't at head; `/livez` stays up | PAY-605 |
| `test_telemetry_redaction.py` | **The redaction test (integration):** run invite → accept → login → reset with real Postgres, Redis and Mailpit, capturing all logs and spans; assert that the raw invite and reset tokens, the passwords, `AUDIT_IP_HASH_KEY`, `REDIS_IDENTITY_PASSWORD`, `IDENTITY_DB_PASSWORD`, the JWT and the raw client IP never appear | PAY-NFR-07 |

### 7.3 `services/identity/tests/contract/`

| Test file | Covers | Requirement |
|---|---|---|
| `test_openapi.py` | The exported spec equals the committed `openapi.json`; every route in §3.4 is present with its `operationId`, request model and documented error responses; no route accepts a token as a query parameter | PAY-NFR-12, PAY-103 |

The Schemathesis run against identity (no 5xx, no schema violations) is in the CI `contract` job (PAY-NFR-12), using tokens printed by `scripts/seed.py`.

## 8. Traceability

| Requirement | Module(s) | Test(s) |
|---|---|---|
| PAY-100 | `bootstrap_owner.py` | `integration/test_bootstrap_owner.py`, `unit/test_bootstrap_args.py` |
| PAY-101 | `api/users.py`, `usecases/invites.py`, `domain/tokens.py`, `email/relay.py` | `integration/test_invites.py`, `unit/test_tokens.py` |
| PAY-102 | `api/invites.py`, `usecases/invites.py` | `integration/test_invites.py` |
| PAY-103 | `api/invites.py`, `usecases/invites.py`, `domain/passwords.py`, `email/templates.py` | `integration/test_invites.py`, `unit/test_passwords.py`, `unit/test_email_templates.py`, `contract/test_openapi.py` |
| PAY-104 | `api/auth.py`, `usecases/signin.py`, `limits.py`, `domain/lockout.py`, `domain/passwords.py` | `integration/test_login.py`, `integration/test_lockout.py::test_lockout_production_values`, `unit/test_lockout.py`, `unit/test_passwords.py` |
| PAY-105 | `api/auth.py`, `usecases/resets.py`, `limits.py` | `integration/test_password_reset.py` |
| PAY-106 | (web LLD) — identity supplies `SessionUser` | `integration/test_invites.py`, `integration/test_login.py` (response shape) |
| PAY-107 | `api/users.py` (`/me`), `domain/users.py` (`roles_version`) | `integration/test_users_admin.py`, `unit/test_user_rules.py` |
| PAY-108 | `api/users.py`, `usecases/users.py`, `domain/users.py` | `integration/test_users_admin.py`, `unit/test_user_rules.py` |
| PAY-109 | `api/audit.py`, `usecases/audit.py`, `db/repositories.py` | `integration/test_audit_log.py` |
| PAY-110 | `email/relay.py`, `email/smtp.py`, `email/templates.py`, `domain/email_schedule.py`, `metrics.py` | `integration/test_email_relay.py`, `unit/test_email_schedule.py`, `unit/test_email_templates.py` |
| PAY-600 | pycommon verifier; `settings.py` keyset; `startup_checks.py` | `unit/test_routes_auth.py`, `unit/test_startup_checks.py` |
| PAY-601 | `api/deps.py` | `unit/test_routes_auth.py`, `integration/test_users_admin.py` |
| PAY-602 | `api/errors.py` (+ pycommon) | `unit/test_api_errors.py` |
| PAY-603 | `limits.py`, `api/deps.py` | `integration/test_rate_limits.py`, `integration/test_lockout.py`, `unit/test_metrics.py` |
| PAY-604 | pycommon body limit (256 KB) | `unit/test_api_errors.py` (413) |
| PAY-605 | `main.py`, `startup_checks.py` | `integration/test_readyz.py`, `unit/test_startup_checks.py` |
| PAY-606 | `migrations/`, `db/models.py` | `integration/test_migrations.py`, `integration/test_grants.py` |
| PAY-607 | `settings.py` (Redis user) | `integration/test_redis_acl.py` |
| PAY-608 | `maintenance.py` | `integration/test_maintenance.py` |
| PAY-609 | `bootstrap_owner.py` (`--password-env`) | `integration/test_bootstrap_owner.py` |
| PAY-NFR-07 | `email/relay.py`, `api/errors.py`, pycommon redactor | `unit/test_redaction.py`, `integration/test_telemetry_redaction.py`, `unit/test_api_errors.py` |
| PAY-NFR-08 | `domain/ip.py`, `maintenance.py`, `db/models.py` | `unit/test_ip.py`, `integration/test_audit_log.py`, `integration/test_maintenance.py` |
| PAY-NFR-09 | `email/relay.py` (`traceparent`), pycommon logging | `unit/test_redaction.py` (trace_id on lines); e2e `trace.spec.ts` (web) |
| PAY-NFR-10 | `metrics.py` | `unit/test_metrics.py` |
| PAY-NFR-12 | all; `openapi.json` | `contract/test_openapi.py`; CI coverage ≥ 85% |

## 8a. Lead resolutions applied (2026-11-10; README R-table)

These override anything earlier in this file that disagrees.

- **R15:** locked accounts get 429 `rate_limited`, as designed.
- **R16:** `bootstrap_owner` gains `--if-empty`: when users exist it exits 0 and logs `identity.bootstrap.skipped` (info). Without the flag it still exits 3. `--password-env VAR` (ENV=dev only, otherwise exit 2) creates an active owner with that password and no invite. Add both cases to `test_bootstrap_owner.py`.
- **R17:** tokens are generated by the relay at send time, as designed.
- **R18:** `X-Client-IP` is trusted only on `iss=web` tokens; `service:web` is exempt from the per-caller limit.
- **R19:** login, accept and `/v1/users/me` return `roles_version`, `status`, `company_id` and `name`; the BFF puts `name` into user tokens (R9).
- **R21:** `UserOut` adds `pending_invite_id: UUID | None`.
- **R25:** replace the gauge `identity_emails_failed` with the counter `identity_emails_failed_total`, incremented when an email becomes `failed`.
- **R3/R4:** generic 422 `validation_error`; lists are `{items, next_cursor}`.
- **R37:** reactivation is out of scope.

## 9. Issues for the lead (all resolved: see the section above and the spec README R-table)

Contradictions and gaps found in the source documents. This LLD takes the stated choice; none of the sources were edited.

1. **Locked account: 401 or 429?** Plan §5 J1 says "a locked account returns the same 401 and counts toward the backoff"; PAY-104 says that past the limits "the response is 429 `rate_limited` with `Retry-After`, and the account locks". This LLD follows PAY-104: 429 while locked, with no argon2 and no extension of the lock. Unknown emails lock the same way, so 429 doesn't reveal whether an account exists. Please confirm, and align the plan sentence.
2. **Bootstrap owner: pending + invite, or a password?** PAY-100 and ADR 0002 say `bootstrap_owner` creates a `pending` owner and emails an invite ("no back door"); PAY-609 says "the `bootstrap-owner` Compose one-shot creates the owner with `SEED_OWNER_PASSWORD`". This LLD adds `--password-env <VAR>`, allowed only when `ENV=dev`, which creates an `active` owner with a password and no email. Please confirm, or instead have `seed.py` accept the owner's invite through Mailpit.
3. **Where does the raw token for the email come from?** Tokens are stored only as SHA-256 (PAY-101), and the outbox holds a "token reference" and never the body (PAY-110), so nothing stored can rebuild the link. This LLD issues the token in the relay at send time (§6.7). Consequence: PAY-101's "creates … an invite token … and queues the email" becomes "creates the invite; the token is generated when the email is sent", and each re-send attempt that reaches SMTP replaces the token. The alternative is to store the raw token encrypted in the outbox, which needs a new key secret.
4. **How does identity learn the client IP?** PAY-104 needs per-IP limits and `ip_hash` in identity's audit, but only the BFF sees the client IP, and no document says how it's passed. This LLD defines the `X-Client-IP` header (required on the four public routes, trusted only with an `iss=web` token). The web LLD must send it.
5. **Format and window of the login limits.** `AUTH_LOGIN_LIMIT_ACCOUNT` / `_IP` are given as "5 / 20 per 15 min" with no value format, and the lockout's 1 → 60 min backoff has no setting names. This LLD uses integers for the counts plus new keys `IDENTITY_LOGIN_WINDOW_S`, `IDENTITY_LOCKOUT_BASE_S`, `IDENTITY_LOCKOUT_MAX_S` and `IDENTITY_LOCKOUT_LEVEL_TTL_S`. `compose.test.yaml` must raise the counts.
6. **Limits for invite-accept and reset-confirm are not set.** PAY-603 says they fail closed but gives no values. This LLD reuses `AUTH_LOGIN_LIMIT_IP` per IP for both.
7. **The per-caller service limit for `service:web`.** PAY-603 keys service limits by `(iss, sub)`. On identity's public routes every anonymous request has `sub=service:web`, so one 600/min bucket would be shared by every visitor and could lock everyone out of sign-in. This LLD exempts `service:web` from `SERVICE_RATE_LIMIT_USER` and relies on the per-IP and per-account limits on those routes.
8. **Mixed env-var prefixes.** The plan names identity settings without a prefix (`INVITE_TTL_H`, `RESET_TTL_H`, `EMAIL_MAX_ATTEMPTS`, `AUTH_LOGIN_LIMIT_*`, `SMTP_*`), while `architecture.md` §3 asks for a per-service prefix. This LLD keeps the plan's names exactly and prefixes only new keys with `IDENTITY_`. `.env.example` should follow the same rule.
9. **User reactivation is not specified.** Plan §7 and PAY-108 cover deactivation only. This LLD accepts only `status: "inactive"` in PATCH; reactivating a user needs a decision (and probably an audit event `user.reactivated`). Related: inviting the email of an inactive user returns 409 `user_email_taken`, so such a person can't come back in 0.1.0.
10. **New error codes not in the plan:** `user_not_found` (404), `invite_not_found` (404) and `validation_error` (422; the generic code isn't named in `architecture.md` §8 or PAY-602). pycommon's LLD should fix the name `validation_error`, and its 422 handler must never echo `input` values (a password or token would leak back into responses and logs).
11. **Audit response fields.** PAY-109 lists `{id, at, actor_user_id, event, target_user_id, ip_hash}`. This LLD adds `details` (for example the old and new roles), which is non-breaking but goes beyond the requirement. Plan §6's audit retention of 400 days has no setting name; this LLD adds `IDENTITY_RETENTION_AUDIT_DAYS`, and uses `RETENTION_OUTBOX_DAYS` for PAY-608's "emails 7 days after they're sent or failed".
12. **`EmailOutboxStuck` "any failed" stays firing for 7 days.** `identity_emails_failed` is a gauge of `failed` rows, which are kept until the 7-day purge, so the alert can't clear sooner. The observability rules should decide whether it fires on `increase()` of a counter instead, or the runbook needs a way to acknowledge failed rows.
13. **pycommon dependencies.** Identity needs pycommon to expose (a) a circuit breaker and `external_requests_total` / `external_request_duration_seconds` helpers usable for a non-HTTP call (SMTP; plan §4 says "through the resilient client"); (b) a maintenance runner taking an advisory-lock name; (c) a rate limiter with a fail-open mode for the per-caller limit.
14. **`last_admin` is defense in depth only.** With `cannot_modify_self` plus the DB check that the actor is still an active admin under the same lock (§6.5), the last active admin can't be removed by any request, so 409 `last_admin` is reachable only if those checks regress. The unit test covers it directly. No change needed unless you want the actor DB check removed.
15. **Password-reset session revocation depends on the BFF.** PAY-105 says confirm "revokes all sessions" and earlier sessions get 401 on their next request. identity can't touch `web:*` (Redis ACL), so it returns `user_id` and bumps `roles_version`; the BFF must delete `web:user-sess:<user_id>` immediately, otherwise sessions survive for up to `SESSION_ROLE_RECHECK_S` (60 s). The web LLD must state this.
16. **Plan §5 J1 sequence vs PAY-101.** The diagram shows `POST /api/users/invite {email, roles}` without `name`; PAY-101 requires `name`. This LLD follows PAY-101.
