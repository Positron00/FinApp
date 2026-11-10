# Spec: PayRun 0.1.0

Status: draft v1 (being aligned with plan v1.2, 2026-11-10) · Source plan: [`docs/plan/architecture-plan.md`](../../plan/architecture-plan.md) · Base: `main` at `0b082dc` (initial commit) · Release: 0.1.0

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
| [`requirements.md`](requirements.md) | PAY-100 – PAY-110, PAY-200 – PAY-204, PAY-300 – PAY-304, PAY-400 – PAY-413, PAY-500 – PAY-502, PAY-600 – PAY-609, PAY-NFR-01 – PAY-NFR-14 |
| [`design-high-level.md`](design-high-level.md) | Components, flows, state machines, data layout, failure table, deployment, security, limits, observability |
| [`design-low-level-identity.md`](design-low-level-identity.md) | identity service |
| [`design-low-level-payroll.md`](design-low-level-payroll.md) | payroll service, plus the event contract |
| [`design-low-level-payout-worker.md`](design-low-level-payout-worker.md) | payout-worker |
| [`design-low-level-bank-stub.md`](design-low-level-bank-stub.md) | bank-stub |
| [`design-low-level-web.md`](design-low-level-web.md) | Next.js pages, BFF, e2e and load tests |
| [`design-low-level-platform.md`](design-low-level-platform.md) | pycommon, bootstrap, Compose, observability configs, Kubernetes, images, CI |
| [`tasks.md`](tasks.md) | Tasks in dependency order, with coverage |
| [`../../../contracts/events/payout.requested.v1.json`](../../../contracts/events/payout.requested.v1.json) | Event schema |

Requirements say *what*; the designs say *how*; tasks say *in what order*. When documents
disagree, `requirements.md` wins, then the designs, then the plan.

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
- a void endpoint at the bank.

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
| R22 | `LOAD_USER_EMAIL` has the role `payroll_admin`. k6 `settlement.js` approves as `approver@payrun.local`. `seed.py` sets tax 2000 bps and retirement 500 bps; the migration seeds 0/0 | N, P | PAY-609 |
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
