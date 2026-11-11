# Spec: PayRun 0.1.0

Status: v1.1 aligned with plan v1.3 (2026-11-10; S1–S7 done; Step 5 alignment review folded in; F1–F6, H1 and the H2 drills done and H3 security fixes applied on `release/0.1.0`; decision tables R1–R37 and C1–C75; traceability clean) · Source plan: [`docs/plan/architecture-plan.md`](../../plan/architecture-plan.md) · Base: `main` at `0b082dc` (initial commit) · Release: 0.1.0

## What this work does

PayRun is payroll for small and medium businesses. A company's staff invite each other, keep an
employee list, and run payroll each period:
1. create a run;
2. enter hours;
3. calculate pay with exact integer-cent rules;
4. review the payslips;
5. approve with dual control.

Each employee's net pay then goes out as a separate payout to a simulated bank. Failed payouts
can be fixed and retried.

The system is a Next.js web app (pages plus BFF) in front of three Python services (identity,
payroll, payout-worker) and a bank simulator. They use Postgres, Redis Streams and a full
OpenTelemetry stack, and deploy with Docker Compose and Kubernetes. The hardest part is that no
employee may ever be paid twice, and every payout must reach a final state (plan §1, ADR 0003).

## Documents

| File | What it holds |
|---|---|
| [`requirements.md`](requirements.md) | PAY-100 – PAY-110, PAY-200 – PAY-204, PAY-300 – PAY-304, PAY-400 – PAY-413, PAY-500 – PAY-502, PAY-600 – PAY-609, PAY-NFR-01 – PAY-NFR-15 |
| [`design-high-level.md`](design-high-level.md) | Components, flows, state machines, data layout, failure table, deployment, security, limits, observability |
| [`design-low-level-identity.md`](design-low-level-identity.md) | identity service |
| [`design-low-level-payroll.md`](design-low-level-payroll.md) | payroll service, plus the event contract |
| [`design-low-level-payout-worker.md`](design-low-level-payout-worker.md) | payout-worker |
| [`design-low-level-bank-stub.md`](design-low-level-bank-stub.md) | bank-stub |
| [`design-low-level-web.md`](design-low-level-web.md) | Next.js pages, BFF, e2e and load tests |
| [`design-low-level-platform.md`](design-low-level-platform.md) | pycommon, bootstrap, Compose, observability configs, Kubernetes, images, CI |
| [`tasks.md`](tasks.md) | Tasks in dependency order, with coverage |
| [`../../../contracts/events/payout.requested.v1.json`](../../../contracts/events/payout.requested.v1.json) | Event schema |
| [`../../../contracts/events/payout.requested.dlq.v1.json`](../../../contracts/events/payout.requested.dlq.v1.json) | DLQ entry schema (C21) |
| [`../../../contracts/events/examples/`](../../../contracts/events/examples/) | Event and DLQ examples, validated by `contract.sh` and read by the contract tests (C57) |
| [`../../adr/`](../../adr/) | ADRs 0001–0013; they rank between `requirements.md` and the designs |

Requirements say *what*; the designs say *how*; tasks say *in what order*. When documents
disagree, the order is: the Contract decisions table (C), then the Spec resolutions table (R), then `requirements.md`, then the ADRs, then the designs, then the plan.

## Scope

**In scope:** journeys J1–J5 (plan §3), the observability stack, every test layer and gate,
Compose and Kubernetes manifests, and CI.

**Out of scope** (plan §2):
- real tax tables or filing;
- multiple states or countries;
- benefits, time off, contractors and overtime;
- real bank rails;
- more than one company;
- self sign-up and email change;
- payslip PDFs and employee self-service;
- 27-period years and holiday or weekend pay-date adjustment;
- statutory retention rules;
- a void endpoint at the bank;
- staff-user reactivation and changing a run's employee set (R37).

Employee reactivation (PAY-203) is in scope. This list is identical to plan §2.

## Decisions (accepted 2026-11-10; plan §16)

| ID | Decision |
|---|---|
| ADR 0001 | Services: identity, payroll, payout-worker, bank-stub (persistent ledger; dev and uat only) |
| ADR 0002 | Sign-in pattern A, invite-only, roles admin, payroll_admin and approver; `bootstrap_owner` CLI |
| ADR 0003 | Payout event out and API back; double-payment guard (a)–(e); indeterminate attempts are never final; re-publish |
| ADR 0004 | Bank fields AES-256-GCM with key IDs; API shows only the last 4 digits |
| ADR 0005 | Dual approval as a company setting, default on |
| ADR 0006 | Integer cents and basis points; rounding rules |
| ADR 0007 | One Postgres, one Redis (noeviction plus AOF), ACL user per service |
| ADR 0008 | `.env` in dev and CI; External Secrets in Kubernetes |
| ADR 0009 | Nonce-based CSP |
| ADR 0010 | Local webhook alert sink |
| ADR 0011 | Default-deny egress with explicit allows |
| ADR 0012 | SLOs: 99.5%, p95 300 ms, settlement ≤ 60 s |

## Notes

- **Brief deviation (recorded in plan §2.1):** approval moves a run straight to `processing`. `approved` is recorded by `approved_at` rather than kept as a status.
- **Toolchain deviations (S1, 2026-11-10).** Versions are the newest stable releases published on or before 2026-10-27 (the two-week rule), except:
  - **pnpm 10.34.9, not 12.16.0:** corepack 0.31 can't load pnpm 12's ES-module entry point.
  - **TypeScript 6.0.3, not 7.0.2:** typescript-eslint 8.72 requires TypeScript below 6.1.
  - **Node:** `engines` allows ≥ 22 locally, because the dev machine has 23.7 and no version manager. CI pins Node 24 LTS via `.nvmrc`.
  - **Python:** 3.14.7, installed by uv.
- Implementation measurements and deviations will be added here.

## Spec resolutions (2026-11-10)

The five low-level designs were written in parallel, and each ended with "Issues for the lead".
The lead's decisions are below. They're **authoritative**: where an issue list or an assumption
in a design disagrees, this table wins, and requirements.md and the plan have been updated to
match. Source: I = identity, P = payroll, W = payout-worker, B = bank-stub, N = web, L = platform.

| ID | Decision | Raised in | Changes |
|---|---|---|---|
| R1 | `PAYOUT_REPUBLISH_AFTER_S` default **360** (the 300 s plan value failed the startup check: 300 < 5 × 60 + 15) | P, W, L | plan §2.2, PAY-407, all designs |
| R2 | Worker → payroll setting renamed `PAYOUT_PAYROLL_CALL_DEADLINE_S` (it used payroll's env prefix) | P | all docs |
| R3 | The generic 422 code is **`validation_error`** everywhere. Added codes: `pay_run_not_found`, `payslip_not_found`, `payout_not_found`, `user_not_found`, `invite_not_found`, `company_settings_not_found`, `employee_not_in_run`, `idempotency_key_required`, `anchor_locked`; BFF only: `invalid_json`, `request_failed` | I, P, N | PAY-602 |
| R4 | List responses are `{items: [...], next_cursor: string \| null}`; the cursor is opaque base64url | N | PAY-602 |
| R5 | Poison (schema-invalid, amount mismatch, `payout_not_found` on the instruction) goes to the DLQ **at once**, with `poison` reported where the payout ID is readable. Payroll unreachable and worker DB failures stay pending, then go to the DLQ after `PAYOUT_MAX_DELIVERIES` | W, L | PAY-408, plan §12.1, HLD §4 |
| R6 | The bank response is `{transfer_id, status, reason, bank_reference}`: 201 for a new transfer, 200 for a replay. The key is matched by `^[A-Za-z0-9:_-]{1,255}$` and looked up **before** the body is validated. bank-stub `/readyz` checks only the DB. Metric `bank_stub_transfers_total{outcome}` added | B | PAY-412, PAY-605, plan §7/§11 |
| R7 | The result endpoint returns `{payout_id, attempt, outcome}`. Stream entries carry the fields `event_id`, `type` and `envelope` (JSON). For a `needs_operator` attempt, a definite `settled`/`rejected` applies (the bank's answer wins); `indeterminate`/`poison` return 200 with no change | W, P | PAY-402, PAY-405 |
| R8 | **New PAY-502, operator resolution** of `needs_operator` attempts: `POST /v1/pay-runs/{id}/payouts/{payout_id}/resolve {action: "redrive" \| "mark_rejected", note}` (approver; audited). The runbook requires checking the bank first (`GET /v1/transfers/{key}`) | P | requirements, payroll and web designs, tasks F4/F5 |
| R9 | Worker JWTs: `roles: []`, `jti`, no `sid`. Public-flow tokens (`sub=service:web`) carry `cid = COMPANY_ID`. User tokens add a `name` claim, so payroll can store `calculated_by_name` and `approved_by_name` for display | W, N | PAY-600, payroll and web designs |
| R10 | Worker → payroll calls also retry on connect errors and read timeouts; both calls are idempotent | W | plan §4 |
| R11 | The timing startup checks: payroll checks re-publish; the worker checks claim idle and re-publish. Both read the shared `payout-timing` keys | W, L | — (already in designs) |
| R12 | Redis ACLs gain: PING for all users; write access for MKSTREAM, plus XINFO on `.dlq` and XGROUP DELCONSUMER for the worker; a read-only exporter user | W, L | ADR 0007 note, platform design |
| R13 | Extra columns accepted: `disbursements` (company_id, reason, last_event_id, deliveries), `transfers` (id, currency, bank_reference, timestamps), `pay_runs.first_final_at` | W, B, P | — |
| R14 | The prod overlay sets `payout-worker` replicas to 0 until a bank-provider ADR exists, so the placeholder `BANK_BASE_URL` check can't crash-loop it | W | plan §13 |
| R15 | A locked account gets **429 `rate_limited`** with `Retry-After`, as PAY-104 says. Unknown emails lock the same way, so no account enumeration | I | plan §5 J1 |
| R16 | `bootstrap_owner --if-empty` exits 0 (no-op) when users exist, so Compose and the Job can re-run. `--password-env VAR` (ENV=dev only) creates an **active** owner with no email; the Compose `bootstrap-owner` one-shot uses `SEED_OWNER_PASSWORD` | I, L | PAY-100, PAY-609 |
| R17 | Invite and reset tokens are **generated by the email relay at send time** (only the hash is stored; each send replaces it). The token's TTL runs from the send | I | PAY-101, PAY-102, PAY-105 |
| R18 | The BFF sends `X-Client-IP` (from `TRUSTED_PROXY_HOPS`). Services trust it only on `iss=web` tokens. `service:web` is exempt from the per-caller service limit; accept and reset-confirm are limited per IP with `AUTH_LOGIN_LIMIT_IP` | I, N | PAY-603 |
| R19 | Login, accept and `/v1/users/me` return `roles_version`, `status` and `company_id`. Reset confirm returns `user_id`, and the BFF deletes every session in `web:user-sess:<user_id>` at once | N, I | PAY-105 |
| R20 | A demoted or deactivated user is ended at the next re-check: 401 per PAY-107. Plan §12.1 is updated | N | plan §12.1 |
| R21 | `GET /v1/users` returns `pending_invite_id` for pending users. Payouts return `payslip_id` and `amount_cents`. The run summary returns `counts.awaiting_bank` and `counts.blocked`, `require_dual_approval`, `calculated_by_name` and `approved_by_name` | N | PAY-108, PAY-303, PAY-501 |
| R22 | `LOAD_USER_EMAIL` has the role `payroll_admin`. k6 `settlement.js` approves as `approver@payrun.example.com`. `seed.py` sets tax 2000 bps and retirement 500 bps; the migration seeds 0/0 | N, P | PAY-609 |
| R23 | Sessions are keyed by an HMAC of the cookie ID (`web:sess:<hmac>`), so a Redis dump has no usable cookie values. Accepted | N | PAY-106 |
| R24 | New web config accepted: `SESSION_RECHECK_GRACE_S` (300 s fail-open while identity is down), `SESSION_TOUCH_INTERVAL_S`, `BFF_TELEMETRY_RATE_LIMIT_IP`, `BFF_BODY_LIMIT_BYTES`, `UPSTREAM_*`. `IDENTITY_PUBLIC_BASE_URL` = the web origin | N, I | — |
| R25 | Email failures: counter `identity_emails_failed_total`. `EmailOutboxStuck` = oldest pending > 900 s, **or** `increase(identity_emails_failed_total[1h]) > 0`, so it clears | I, L | plan §11, identity and platform designs |
| R26 | Gauges are computed **at metric collection** (OTel observable callbacks, 15 s export interval), not at Prometheus scrape time | P, L | plan §2.1/§11 wording |
| R27 | **Prod sampling:** the apps send 100% of spans (`parentbased_always_on`); the Collector's `tail_sampling` keeps all errors, traces slower than 300 ms, and 10% of the rest | L | plan §2.2/§11 |
| R28 | `PodRestarts` is evaluated only in Kubernetes, where kube-state-metrics is a listed cluster dependency. Its promtool test still runs in CI | L | plan §11, §13 |
| R29 | ExternalSecrets are used in uat and prod (ADR 0008); the HLD is corrected | L | HLD §5 |
| R30 | Schemathesis gets a fresh token per request from an auth hook (`scripts/mint_token.py`), because 60 s JWTs make printed tokens useless. PAY-609 is updated | L | PAY-609 |
| R31 | alert-sink is published at host port 4135 (test override) | L | plan §13 |
| R32 | The bootstrap SQL is copied into `deploy/k8s/base/bootstrap/` by `scripts/sync-k8s-assets.sh`, and CI fails if the copy differs. No kustomize load-restrictor override | L | — |
| R33 | `biweekly_anchor` can't change once any non-deleted biweekly run exists: 409 `anchor_locked` | P | PAY-200 |
| R34 | Accepted as designed: approving with purged bank details gives 409 `employee_bank_details_missing`; retry's dual-approval check uses the run snapshot; creating an employee counts as a bank edit; `republish_seq` counts before the increment (0 first); wrong issuer gives 401 and a missing role 403; the overlap rule may block a period until the moved employee is deactivated or moved back (documented limitation) | P | — |
| R35 | Accepted label additions: `provider="payroll"`; † alert expressions are reviewed in H2 | L | — |
| R36 | `TRUSTED_PROXY_HOPS=0` on Next 16 gets a short spike in S7. If the peer address is unavailable, local dev uses `X-Forwarded-For` from the Next server, and the result is recorded here | N | task S7 |
| R37 | User reactivation and changing a run's employee set are out of scope for 0.1.0 | I, P | README scope |

## Contract decisions (2026-11-10, after the Step 2 reviews)

These come from two independent reviews: cross-service contracts (X#) and plan alignment (A#).

**Precedence:** this table > the Spec resolutions R-table > requirements.md > the ADRs > the designs > the plan. The design bodies have been rewritten to match, so there are no override-only addenda left.

| ID | Decision | From |
|---|---|---|
| C1 | **Env vars:** pycommon `BaseServiceSettings` names are the contract for every Python service: `DB_HOST`, `DB_PORT`, `DB_NAME` (`payrun`), `DB_USER`, `DB_PASSWORD`, `DB_SCHEMA`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD`. The migrate container sets the same keys with migrator credentials. Service-only keys keep a service prefix (`IDENTITY_`, `PAYROLL_`, `PAYOUT_`, `BANK_STUB_`). No `DATABASE_URL`/`REDIS_URL` | X1 |
| C2 | **DB roles** are `<schema>_migrator` and `<schema>_app` (`identity_app`, `payroll_app`, `payouts_app`, `bank_stub_app`) | X2 |
| C3 | **No Lua in Redis.** identity's limiter and lockout use pycommon `RateLimiter` (MULTI with INCR and EXPIRE NX), plus `SET NX EX` for lock levels. The ACLs keep `-@scripting` | X3 |
| C4 | **Web container:** port 3000 inside (`PORT=3000`), host `WEB_PORT` 4100 → 3000. Health routes `/api/livez` (liveness) and `/api/readyz` (readiness) everywhere: Compose healthcheck, Kubernetes probes, seed.py, and the SLI exclusion `route!~"/api/livez\|/api/readyz\|/api/telemetry"` | X4, A1 |
| C5 | **BFF Zod response schemas mirror the service Out models field for field** (strict stays, to block leaks):<br>• `Employee` adds `has_bank_details` and `bank_updated_by`, with a nullable `account_last4`.<br>• `Payout` = payroll `PayoutOut` (`id, pay_run_id, payslip_id, employee_id, employee_name, amount_cents, currency, status, no_transfer, current_attempt, attempts[]`), and `Attempt` = `AttemptOut` (including `account_last4`).<br>• `Payslip` has no `schedule`, and its amount fields are nullable while pending.<br>• `PayrollAuditEvent` = `{id, at, actor_id, event, entity_type, entity_id, details}` | X5, X6, X7, X10 |
| C6 | **Login and accept:** identity returns `SessionUser {user_id, company_id, email, name, roles, roles_version, status}`. The BFF parses it with a `SessionUser` Zod schema and maps it to `Me` (`id = user_id`) | X8 |
| C7 | `PUT …/hours` returns `HoursOut`; the BFF route returns it, and the UI refetches the run | X9 |
| C8 | **Every list is paginated** `{items, next_cursor}`, including payslips and payouts. The BFF passes `cursor` and `limit` through and returns `Page<T>`. The UI pages, and k6 and the hours editor loop until `next_cursor` is null | X11, A21 |
| C9 | **Run summary:** `PayRunOut.counts = {requested, awaiting_bank, blocked, paid, no_transfer, failed}`, plus `calculated_by_name`, `approved_by_name` and `require_dual_approval`. The web `PayRun` mirrors it | X12 |
| C10 | **`name` claim:** the session record stores `name` (from `SessionUser`, refreshed on the `/me` re-check). `internalToken()` adds it for user tokens. pycommon `Principal.name: str \| None`. PAY-106 lists `name` | X13 |
| C11 | identity `UserOut.pending_invite_id: UUID \| None` is a flat field; the web `User` uses the same name | X14 |
| C12 | **Dev origin:** `PUBLIC_BASE_URL` = `IDENTITY_PUBLIC_BASE_URL` = `http://127.0.0.1:${WEB_PORT}`, and every test and browser uses 127.0.0.1. The web uses the C1 Redis names (`REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME=web`, `REDIS_PASSWORD`) | X15 |
| C13 | **Redrive:** `publish_count = 1`, `republish_seq = 0`, `last_published_at = now()`. PAY-502 is updated | X16, A2 |
| C14 | **`validation_error` details** are `[{field, issue}]` everywhere and never echo input | X17 |
| C15 | **The BFF error catalogue** adds `employee_not_in_run`, `idempotency_key_required`, `company_settings_not_found`, `payslip_not_found`, `not_found`, `method_not_allowed`, `bad_request`, `payout_not_blocked` and `anchor_locked`. It forwards `details.employee_ids` for `not_hourly`, `employee_not_in_run`, `employee_in_overlapping_run` and `hours_missing` | X18 |
| C16 | **Client IP:** the BFF omits `X-Client-IP` when the IP is unknown. identity puts requests without it into one shared `unknown` per-IP bucket | X19 |
| C17 | **Caller limit exemption:** pycommon `caller_rate_limit(exempt_subs=("service:web",))`. identity uses pycommon's key format | X20 |
| C18 | **Gauges:** payroll design §7.2 (collection-time callbacks) is authoritative for payroll's gauges, including `outbox_*`. pycommon offers the callback helper only | X21 |
| C19 | `auth_signins_total{outcome}` takes `success`, `failure` or `rate_limited`. Alerts, promtool tests and dashboards use `failure` | X22 |
| C20 | The platform body uses `identity_emails_failed_total` everywhere (R25) | X23, A9 |
| C21 | **Queue consumer and DLQ:**<br>• The worker keeps its own `Consumer`, but pre-initialises the `queue_messages_total` series with `add(0)` for `ok`, `retry` and `dlq` at start.<br>• The DLQ entry fields are `envelope, source_id, error_kind, error_code, error, delivery_count, consumer, failed_at`, with `MAXLEN ~ 10000`, specified in `contracts/events/payout.requested.dlq.v1.json`.<br>• The outcome labels are `ok`, `retry` and `dlq` | X24, A22 |
| C22 | **pycommon streams:**<br>• pycommon defines no outbox, idempotency or processed-events tables: payroll's and the worker's DDL own them.<br>• pycommon provides `StreamProducer.publish(stream, *, event_id, type, envelope_json, maxlen)`, which writes the three fields (R7), plus a relay loop over a repository protocol.<br>• `Envelope.traceparent` is required | X25 |
| C23 | **Trace sampling:** `OTEL_TRACES_SAMPLER=parentbased_always_on` in every service and environment; there's no `OTEL_TRACES_SAMPLER_ARG`. Prod sampling is the Collector's `tail_sampling` (R27) | X26, A5 |
| C24 | **`LOG_LEVEL`** is case-insensitive (pycommon upper-cases it before validating); the docs use uppercase | X27 |
| C25 | **No user reactivation:** the web `UserPatchBody.status` is `"inactive"` only, with no "Reactivate" action. Employee reactivation (PAY-203) stays in scope | X28, A14 |
| C26 | **Result response:** `outcome` takes any attempt status (`requested`, `indeterminate`, `needs_operator`, `settled`, `rejected`). The worker treats `settled` and `rejected` as final and everything else as recorded. `bank_reference` ≤ 64 characters everywhere | X29 |
| C27 | **Redis ACLs:** the worker gets `+xgroup\|delconsumer`. `seed.py` gives `LOAD_USER_EMAIL` the role `payroll_admin` only. The dev owner is active (R16), and seed signs in as it | X30, A9 |
| C28 | **Dev `COMPANY_ID`** = `0193a000-0000-7000-8000-000000000001` everywhere, including the event-schema examples | X30, A6 |
| C29 | **Startup checks:** every service uses pycommon's: exit 2 and log event `startup.check_failed`. The web logs `config.invalid` and exits 2 | X30 |
| C30 | **Auth status codes:**<br>• an invalid or expired token, or an issuer/kid mismatch → 401 `invalid_token`;<br>• a valid token of the wrong kind for the route (for example `service:web` on a user route) or a missing role → 403 `forbidden_role` | X30 |
| C31 | **Token TTL:** invite and reset token TTLs start at send (R17); identity's `expires_at` is set when the relay sends | X30 |
| C32 | **Prod overlay:** payout-worker replicas 0 (R14), with a `test_k8s_policy.py` assertion | A3 |
| C33 | **Skeleton seed:**<br>• `seed.py --skeleton` creates owner and approver users and 5 salaried monthly employees, then one calculated monthly run for the current month (plus a draft run for smoke reads).<br>• The settings PUT moves into S5. The full seed (hourly, both schedules) runs from F3 on.<br>• k6 smoke reads the seeded run's ID from seed's JSON output | A4 |
| C34 | **A missing `Idempotency-Key`** is **400 `idempotency_key_required`** at payroll, bank-stub and the BFF. A malformed key is 422 `validation_error` | A7 |
| C35 | **Token minting:**<br>• `scripts/mint_token.py`: a CLI, plus an importable `mint(aud, sub, roles, *, issuer="web" \| "payout-worker", cid=COMPANY_ID, name=None)`.<br>• The Schemathesis hook is `scripts/ci/schemathesis_auth.py`, which imports it.<br>• Playwright's test-only helper calls the CLI. | A8 |
| C36 | **No kustomize load-restrictor override** (R32): manifests.sh builds overlays normally from the synced copy | A9 |
| C37 | **Identity fail-open window:** with identity unreachable, the BFF keeps cached roles for up to `SESSION_RECHECK_GRACE_S` (300 s), then returns 503. Recorded in plan §8, ADR 0002 and PAY-107 | A12 |
| C38 | **New PAY-NFR-15** covers recovery (RPO 5 min, RTO 1 h) and startup (ready ≤ 20 s after migrations). PAY-106 adds Redis-down → 503. PAY-NFR-12 adds the promtool/amtool gate, 1–2 fault smoke tests, and the Trivy ignore policy. PAY-NFR-05 adds "no memory or lag growth in soak". The nightly failure-issue rule is added to PAY-NFR-12 | A13 |
| C39 | **One out-of-scope list** in plan §2 and the README (including R37 user reactivation and changing a run's employee set). Employee reactivation stays in scope | A14 |
| C40 | PAY-400 lists 409 `employee_bank_details_missing`, and so does plan J4 | A15 |
| C41 | **Task changes:**<br>• F4 is split into F4a (payroll robustness, after F3) and F4b (payout-worker robustness, after S10).<br>• tasks.md gets a "Parallel with" column.<br>• S6 depends on S5 (payroll OpenAPI for its provider tests).<br>• The graph matches the Depends column | A16, A17 |
| C42 | **The HLD state machine** adds `needs_operator → settled \| rejected` (late bank answer), `needs_operator → requested` (redrive) and `needs_operator → rejected` (mark_rejected), and the run `deleted` state | A18 |
| C43 | **Failure table:** HLD §4 is the source, and plan §12.1 is updated to the same log event and metric names | A19 |
| C44 | **Low-level design gaps:**<br>• The bank-stub design adds a spans section.<br>• The web e2e env adds `PAYOUT_WORKER_JWT_PRIVATE_KEY` and `PAYROLL_PORT` (test-only) for the blocked case.<br>• `test_payout_model.py` is owned by payroll only.<br>• Maintenance log events use pycommon's names (`maintenance.task.completed`, `maintenance.task.failed`) | A20 |
| C45 | **Requirements quality:**<br>• Every requirement has an Acceptance bullet.<br>• PAY-609's check is `tests/platform/test_seed.py` plus the e2e global setup.<br>• PAY-107: 401 within 60 s of the change while identity is reachable.<br>• PAY-104: the median latency of unknown-email and wrong-password logins differs by ≤ 20% (integration test) | A22 |
| C46 | The worker's payroll-client settings use the `PAYOUT_PAYROLL_` prefix: `PAYOUT_PAYROLL_BASE_URL`, `_CONNECT_TIMEOUT_S`, `_READ_TIMEOUT_S`, `_MAX_RETRIES` (the C1 prefix rule) | worker patch |
| C47 | Every Python service, bank-stub included, gets `COMPANY_ID` (`BaseServiceSettings` requires it). Accepted: bank-stub's trimmed four-field transfer response (R6); new worker key `PAYOUT_CONSUMER_GC_IDLE_S` (3600) for pruning idle consumers; startup reason `not_deployable` | worker patch |
| C48 | **`mark_rejected` race:**<br>• payroll stores `payout_attempts.needs_operator_at` and refuses `mark_rejected` with 409 `resolve_too_early` and `Retry-After` until `PAYOUT_MARK_REJECTED_MIN_WAIT_S` (120 s) after it.<br>• The worker never starts a bank POST later than `HANDLER_DEADLINE_S` (35 s) after its instruction fetch.<br>• A startup check requires wait > `HANDLER_DEADLINE_S` + 30.<br>• Redrive has no wait (same key). | payroll patch |
| C49 | SMTP settings are unprefixed infrastructure keys (`SMTP_HOST`, `SMTP_PORT`, `SMTP_STARTTLS`, `SMTP_USERNAME`, `SMTP_PASSWORD`), like `DB_*` | identity patch |
| C50 | `10-identity.sql` re-revokes `UPDATE, DELETE` on `identity.audit_log` from `identity_app` after its grants (guarded by `to_regclass`), so a bootstrap re-run can't restore them. The identity migration also revokes, and `test_grants.py` asserts it after a re-run | identity patch |
| C51 | **e2e suites:** until F3, e2e runs only `@skeleton` (`skeleton.spec.ts`: sign in, calculate, approve as the approver, run `paid`, trace) on `seed.py --skeleton`. The J1–J5 `@journeys` specs run from F6 on the full seed. `e2e.sh` takes `E2E_SUITE=skeleton\|full` | web patch |
| C52 | Retry sends an `Idempotency-Key` per click, as approve does. Accepted from the web patch: identity-down tests stop the container (only 4 Toxiproxy proxies), and the BFF parses identity `MeOut` for the re-check | web patch |
| C53 | `compose.test.yaml` keeps production payout timing (claim idle, deadlines, re-publish), because payroll's re-publish check reads the same keys. Only in-process integration fixtures lower them. The stack-level bank-down resilience test is nightly-only (about 5 min) | platform patch |
| C54 | Accepted: both seed modes create `LOAD_USER_EMAIL` (k6 smoke signs in as it) and make sure a current-month calculated run and a draft run exist (`runs.current`, `runs.draft` in the JSON summary). The identity Deployment memory limit is 512Mi (argon2 at 64 MiB × concurrency) | platform patch |
| C55 | pycommon's labels are authoritative. The caller limiter uses scope and component `caller`. `external_requests_total{outcome}` takes `ok`, `client_error`, `rate_limited`, `server_error`, `timeout`, `connect_error` or `breaker_open`. The BFF's own `bff_upstream_requests_total` keeps its labels | platform patch |
| C56 | The `validation_error` `details[].field` is the request location joined by dots, as FastAPI reports it: `body.<path>`, `query.<name>`, `header.<Name>` (e.g. `header.Idempotency-Key`, `body.entries.3.hours_hundredths`). The BFF uses the same form for its own Zod errors | final review M6 |
| C57 | Event examples live in lead-owned files `contracts/events/examples/payout.requested.v1.*.json` and `payout.requested.dlq.v1.*.json`, created with S5. They're validated by `contract.sh` and read by producer and consumer contract tests | final review M10 |
| C58 | **Test and dependency hygiene (S2):**<br>• pytest-socket auto-loads, so gate commands use `--disable-socket --allow-unix-socket` without `-p pytest_socket`.<br>• The two-week rule is enforced in the tools: root `[tool.uv] exclude-newer = "2026-10-28T00:00:00Z"` and `pnpm-workspace.yaml` `minimumReleaseAge: 20160`.<br>• `uv sync --all-packages` installs the workspace members.<br>• `pycommon.db.new_id()` (stdlib `uuid7`) is the ID helper; there is no `pycommon.ids` | S2 |
| C59 | **S4 follow-ups:**<br>• Dev and seed accounts use the RFC 2606 domain `payrun.example.com` (email-validator rejects `.local`), with no process-wide email-validator patch.<br>• The identity log field is `lock_level` (`level` is a base log field).<br>• pycommon `check_secrets` skips optional secrets that default to `None`; services enforce their own prod-required secrets.<br>• `export_openapi.py` builds apps with `instrument=False` | S4 |
| C60 | **S5 follow-ups:**<br>• An Idempotency-Key is scoped per caller, route and path parameters (payroll §4.8), so the same key on another run is a new key.<br>• pycommon maps SQLSTATE `55P03` (lock_timeout) to 503 `db_timeout`, like `57014`.<br>• The payroll internal endpoints and the relay validate envelopes against the JSON Schema while `PAYROLL_VALIDATE_EVENTS` is true | S5 |
| C61 | **S6 follow-ups:**<br>• pycommon instruments SQLAlchemy with `skip_dep_check=True`: instrumentation 0.66b1 gates on sqlalchemy < 2.1, but works on 2.1.6 (verified; `test_db_spans.py`).<br>• Internal-JWT `jti` is UUIDv7.<br>• The DLQ example file is renamed `payout.requested.dlq.v1.payroll-unavailable.json` to match its content.<br>• **Flake, closed as not reproduced (H1, 2026-11-11):** `services/bank_stub/tests/unit` failed once on 2026-11-10 right after heavy integration runs. It didn't reproduce in 16 runs then, nor in 20 more in H1 (10 with the CI flags straight after the integration suite). The original output wasn't captured, so the cause is unknown; no retries were added. If it recurs, capture the output and reopen.<br>• **Isolation gap, closed (H1, 2026-11-11):** two payout-worker integration tests used to fail only when the unit, integration and contract suites ran in one pytest process, because OTel metric globals leaked between suites. They now pass in one process: `pycommon.testing` keeps one provider set (C63) | S6 |
| C62 | **S7 follow-ups (web; overrides web LLD where they differ):**<br>• **R36 result:** Next 16 sets `X-Forwarded-For` to the socket peer only when it's absent, and never appends. The client IP is entry `n − TRUSTED_PROXY_HOPS` for hops ≥ 1, or the right-most entry for hops = 0 (dev). uat and prod refuse `TRUSTED_PROXY_HOPS < 1` (`config.invalid`, exit 2). Details in `apps/web/NOTES.md`.<br>• The ioredis client uses `enableReadyCheck: false`, because the `web` ACL denies INFO.<br>• There's no separate connect timeout: the per-attempt timeout covers connect and read (undici 8 needs Node ≥ 24).<br>• Logs go to OTLP via the OTel logs SDK, not `pino-opentelemetry-transport`. HSTS is set in `proxy.ts`. Process singletons live on `globalThis`.<br>• `/api/auth/me` returns 503 while the re-check is deferred (C37 window).<br>• OTel SDK 2.12 / 0.223 to match the @vercel/otel 2.x peer ranges. jsdom is pinned to 29.1.1 for local Node 23 | S7 |
| C63 | **Step 5 alignment review, lead decisions** (five independent reviews of S1–S7, 2026-11-10):<br>• **Identity:** `purge_audit_log` clamps the cutoff to at most `now() − 400 days`, so the runtime role can't wipe the log. The per-account login limit counts **failed** attempts only (the per-IP limit counts all). The relay checks the SMTP breaker **before** issuing a token. The email gauges are implemented now.<br>• **Web:** the `next` redirect resolves to a same-origin path via `new URL`, rejecting control characters. `proxy.ts` no longer gates `/api/*` (route handlers return 401 with every standard header); pages keep the redirect. Telemetry doesn't spend the session budget. The re-check fails open only on 503, 429, timeout or breaker-open; identity 500 → 503 without fail-open; identity 401 `invalid_token` → log `bff.upstream_auth_failed` and return 503 without deleting the session.<br>• **Worker:** slots are reserved before XREADGROUP/XAUTOCLAIM, so it never holds more than `PAYOUT_CONCURRENCY` messages. On `attempt_not_current`, a stored definite bank answer is still reported (R7). After a non-NOGROUP Redis error it resumes with `>`. Only a body with `transfer_not_found` counts as bank 404 not-found. Keys use `fullmatch`. An instruction that fails pattern validation → `malformed_response`, never leaked into tracebacks. The bank-stub failure threshold uses the exact PAY-412 comparison.<br>• **Payroll:** text fields reject NUL (and pycommon maps SQLSTATE 22021 → 422 `validation_error`). `ResultIn.attempt` ≤ 32767. The relay re-validates envelopes when `PAYROLL_VALIDATE_EVENTS`. `pay_run_exists` returns the real ID on the race path. The overlap check (PAY-300) moves from F3 into F2, before schedule edits.<br>• **pycommon/platform:** gauges never use unit `"1"` (Prometheus would add `_ratio`). Only DB-driver errors map to 503 `db_*` (no bare OSError/TimeoutError). Coverage `fail_under` lives in each member's pyproject (90 pycommon, 85 services). The bootstrap re-run re-revokes `alembic_version` writes. The Redis base ACL denies SCAN and RANDOMKEY. Missing JWT keys refuse start in every env. Test secrets are generated at runtime. `RelayLoop` logs `outbox.relay.published`. `RateLimiter.record_denied(scope)` and `record_error()` replace service-local counters.<br>• **Docs:** ADRs join the precedence chain (C > R > requirements > ADRs > designs > plan), and every design body folds in C53–C63 | Step 5 |
| C64 | **Coverage gate mechanics:** each member's `[tool.coverage.report] fail_under` (90 pycommon, 85 services) applies to the **combined** unit + integration report. The unit step runs with `--cov-fail-under=0`, and the integration step with `--cov-append --cov-fail-under=<threshold>`. `scripts/ci/python.sh` follows this | Step 5 |
| C65 | **S8 decisions:**<br>• **DB time budget:** `DB_STATEMENT_TIMEOUT_MS/1000 + DB_POOL_TIMEOUT_S` (4 s) bounds a request's whole unit of DB work in pycommon `transaction()` and `map_db_errors()`. A repeated-cancel deadline means cleanup can't wait on a slow link; past it, 503 `db_timeout`. asyncpg `command_timeout` = statement timeout + 0.5 s. `statement_timeout` alone doesn't bound network round trips.<br>• Every body-taking route documents 400 `bad_request`, and 405 `Allow` lists every method on the path.<br>• Schemathesis runs all checks except `positive_data_acceptance` (random tokens, cursors and emails are rightly rejected), plus `negative_data_rejection` on bank-stub `POST /v1/transfers` (R6 replays by key). Its stateful phase runs after Playwright.<br>• `compose.test.yaml` sets `BANK_STUB_FAILURE_RATE=0`, so only accounts ending `0000` fail and e2e stays deterministic. The Redis tmpfs is `mode=0700,uid=999,gid=1000`.<br>• Collector 0.164 self-metrics have no `_total` suffix: `check_metrics.sh` expects `otelcol_exporter_sent_spans`, and the F4a gauges aren't expected until F4a.<br>• Images are pinned by version (digests at release). Measured on the dev box: startup about 1.2 s per service; k6 smoke p95 about 30 ms | S8 |
| C66 | **S9 decisions:**<br>• Runtime images drop pip (Python) and npm/corepack (web) and apply Debian updates in the final stage.<br>• 17 Debian base-image CVEs with no fixed version are accepted in `.trivyignore` and `SECURITY_DEPENDENCIES.md`, each with a reason, owner and expiry (2027-01-10; zlib in the web image 2026-12-10). Follow-up: move the web image to a Debian 13 Node base.<br>• Actions are pinned to full SHAs, and Dependabot has a 14-day cooldown.<br>• `nightly-load.sh` runs smoke in place of full/soak/settlement until F6/H2 (labelled stub).<br>• The `stack` pytest marker is registered at the root.<br>• **Still owed:** `docs/runbooks/` (H2); `tests/platform/test_{seed,compose,rules_meta,dashboards}.py` (H1); the identity-down fault smoke test (H1) | S9 |
| C67 | **Shutdown flush bound (F4b finding):** Python services set `OTEL_EXPORTER_OTLP_TIMEOUT=3`. The Python gRPC exporter reads it in **seconds** (default 10), so with the Collector unreachable the telemetry flush can't push payout-worker's 60 s drain past the 75 s grace period. The web app's JS exporter is unaffected | F4b |
| C68 | **F1 settlements (identity):**<br>• Lock order is the user row first, then invites or resets, in every flow (accept, reset confirm, resend, PATCH, deactivate), so there are no deadlocks.<br>• `last_admin` can't be reached through the API (the self rule plus the in-transaction actor re-check); it stays as defence in depth.<br>• Resend and PATCH hash `X-Client-IP` into audit rows.<br>• The `reset_invalid` reasons are `malformed`, `unknown`, `expired`, `not_pending` and `user_not_active`.<br>• Reset request answers 202 with no body | F1 |
| C69 | **F5 decisions (web):**<br>• ADR 0013: local WAI-ARIA primitives (dialog, tabs, field) instead of shadcn/Radix for 0.1.0.<br>• Reset confirm on a Redis failure still answers 204 and logs `auth.session.revoke_failed`; the 60 s `roles_version` re-check ends the sessions.<br>• PAY-603 shared counts are proven with two module graphs over one fake store. A real-Redis web rate-limit test moves to H1 (web Testcontainers or the stack).<br>• The BFF doesn't forward payroll audit filters or `pay_run_exists.details.pay_run_id` (the UI finds the run in the list) | F5 |
| C70 | **F6 decisions:**<br>• Schemathesis's stateful phase damages seed data (deletes runs, changes settings and users), so `e2e.sh` runs Playwright, `test_seed` and k6 smoke **before** Schemathesis. Any nightly job that reads the seed must not follow Schemathesis without re-seeding from a clean stack.<br>• The J5 spec builds its own `partially_failed` run, so specs stay independent.<br>• The soak lives in `load/soak.js`.<br>• `nightly-load.sh` checks soak drift (last vs first 5 min p95 within 20%) and cross-checks settlement with payroll's histogram.<br>• **Measured on the dev box:** 500-employee settlement p95 5.7 s (SLO 60 s); approve p95 207 ms; calculate p95 47 ms; a shortened k6 full run at about 34 req/s, 0% errors, p95 21 ms. The full 10-minute and 30-minute runs belong to the nightly job (H2) | F6 |
| C71 | **Step 5 release review (F1–F6), lead decisions:**<br>• **Late settlement (possible double payment):** a `settled` answer for a stored `rejected` attempt (any reason) or a superseded attempt never changes state or the response (200 stored / 409 `attempt_not_current`). It is recorded once per payout and attempt (audit `payout.late_settlement`), logged at ERROR, counted (`payroll_late_settlements_total`, pre-initialised) and pages (`PayoutLateSettlement`, runbook `docs/runbooks/PayoutLateSettlement.md`).<br>• **Payroll locking and time:** an envelope refused by the relay moves the attempt to `needs_operator` with reason `envelope_invalid`. Re-publish locks payouts in id order, not runs. `needs_operator_at` uses `clock_timestamp()`, and re-publish re-reads the clock after taking locks.<br>• **Accepted risk (replaced by C74):** an identity reset request for an active user is measurably slower than for an unknown email. The per-IP and per-account reset limits slow probing; revisit if the limits are raised (identity LLD §6.6).<br>• **The soak checks PAY-NFR-05 resources** (`scripts/ci/soak_resources.py`): RSS growth < 5% per Python service (web exports no RSS series), and no growing `queue_consumer_lag`. A missing series fails.<br>• **Nightly timeouts:** load 100 min and e2e-repeat 120 min (to be measured in H2). `e2e.sh` fails if seed tests or fault smoke skip.<br>• **Corrections to C65/C66:** e2e runs Playwright, then `test_seed`, k6 smoke, Schemathesis, fault smoke and `check_metrics` (C70 order); the nightly load is no longer a stub (F6) | Step 5 |
| C72 | **H2 decisions:**<br>• Collector `metric_expiration` is 2 m (was 10 m). Each Python process exports per-instance series every 15 s, so a restarted process's stale gauges must expire fast, or `max()`-based paging alerts linger about 10 min after recovery.<br>• Dashboards are grounded in `deploy/observability/metrics-catalog.json`, checked against the code in both directions.<br>• 18 runbooks, one per alert (including PodRestarts).<br>• Counters that only appear on first use (`payroll_payouts_republished_total`, `rate_limited_total`, `bff_sessions_revoked_total`) back no alert, so they aren't pre-initialised.<br>• The PayoutsStuck drill takes about 22 min | H2 |
| C73 | **H3 security review, payroll (four-eyes):**<br>• PAY-401 is tightened (ADR 0005 amendment): with dual approval on, the approver differs from the calculator, the run's creator, any hours editor on the run, and the editor of any bank change not yet vetted. A change is vetted when a payout for that employee was released under dual approval by someone other than the editor. The old window ("since the run was created") let one person edit bank details or add a ghost employee first, then approve another's calculation. Retry uses the same window.<br>• `payout_attempts.resolved_by` is recorded, and the operator who marks an attempt rejected cannot retry that payout (rule `resolver`).<br>• `redrive` of a `key_window_expired` attempt is refused (422 `validation_error`, `body.action` `redrive_not_allowed`): the worker may only look the key up, and the lookup keeps answering 404, so it looped back to `needs_operator`. Only `mark_rejected` after a bank-statement check is left (runbook PayoutNeedsOperator). After key expiry nothing re-asks the bank about the old key, so PayoutLateSettlement can't catch a missed settlement; the statement check is the control. A required statement-reference field on resolve is deferred (a contract change); the runbook asks for it in the note.<br>• `payroll.audit_events` is append-only for `payroll_app` (migration 0003, `20-payroll.sql`), as identity's is (C50).<br>• Denied approves and retries write audit rows `payrun.approve_denied` and `payout.retry_denied` in their own transaction | H3 |
| C74 | **H3 security review, identity:**<br>• A confirmed password reset or an accepted invite clears the account's login lockout (lock, level and the current window's failure count), after the commit and best effort. Anyone who knows an email could otherwise keep that account (even the only admin) locked, and the reset gave no way out. Only the mailbox owner holds the token, so this reveals nothing. Remaining trade-off: the lock is still keyed by email alone, so an attacker can re-lock it, starting from level 1 (identity LLD §6.4).<br>• A reset request pads its lookup and writes to a 50 ms floor (`REQUEST_FLOOR_S`), so a match and a miss take the same time. Measured medians went from a 55% gap to 0.2–1.2%, and `test_reset_request_timing` holds it at ≤ 20%, like the login timing test (C45). Raise the floor if the production p50 nears it | H3 |
| C75 | **H2 alert drills:** PayoutDLQ didn't page in the live drill. The worker's first metric export came 20 s after the poison message was dead-lettered, so the first sample Prometheus saw was already 1, and `increase()` reads a series that starts above 0 as no increase. The same gap hits any restarted process whose first export interval contains the event, despite the C21 pre-initialisation. PayoutDLQ, PayoutLateSettlement and EmailOutboxStuck now also fire on a series that is above 0 and didn't exist one window ago (`X > 0 unless X offset <window>`). New promtool cases cover a series that starts at 1, and its clear after the window. The earlier test that recorded this as an accepted pitfall is removed. The Queues panels (dead-lettered, late settlements, email failures) had the same blind spot and now count new series too.<br>• **Drill results (2026-11-11, `scripts/ci/alert_drills.sh` on the Compose stack):** every paging alert reached alert-sink `/page` with its runbook link, its dashboard queries showed the cause, and the fault cleared. PayoutNeedsOperator 105 s, PayoutLateSettlement 45 s, PayoutSettlementSlow 378 s, PayoutsStuck 1250 s (by design: 600 s stuck threshold + `for: 10m`), BffAvailabilityFastBurn 504 s. PayoutDLQ failed first (this decision), then passed after a worker restart, the original failure condition: paged in 41 s; the full drill took 865 s, mostly waiting for the earlier alert to clear | H2 |
