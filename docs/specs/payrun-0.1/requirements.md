# Requirements: PayRun 0.1.0

Each requirement has an ID, a statement, acceptance criteria and a check. All are "must" unless
marked "should". The source is the [architecture plan](../../plan/architecture-plan.md) (v1.2,
accepted 2026-11-10); plan sections are cited as "plan §n". Each limit and default lives in
exactly one requirement; others refer to it by ID.

**Terms:**
- **Company:** the tenant. 0.1.0 has one, `COMPANY_ID`; every row carries `company_id`, and every query is scoped by the token's `cid`.
- **Staff user:** a person who signs in, with roles `admin`, `payroll_admin` or `approver`.
- **Employee:** a person who is paid. Employees don't sign in.
- **Run:** a pay run for one schedule and period.
- **Payslip:** one employee's lines in a run.
- **Payout:** one payslip's payment. It has one or more attempts.
- **Attempt:** one try to pay a payout. It has one bank idempotency key: `<payout_id>:<attempt>`.
- **Definite rejection:** a bank response that is HTTP 2xx with body `status: rejected` and a `reason`.
- **Final (attempt):** `settled` or `rejected`.
- **Final (payout):** `paid` or `failed`.
- **Final (run):** `paid` or `partially_failed`.

## 1. Access (J1): identity service and BFF sessions

**PAY-100 Bootstrap owner (must).** `python -m identity.bootstrap_owner --email <e> --company-id <uuid>` creates the first user with roles `[admin, payroll_admin, approver]` and status `pending`, and queues an invite email.
- Acceptance:
  - It succeeds only while `identity.users` is empty. Otherwise it exits with code 3 and logs `identity.bootstrap.refused`; with `--if-empty` it exits 0 instead, so Compose and the Kubernetes Job can re-run it.
  - `--password-env VAR` (only when `ENV=dev`) creates an **active** owner with that password and sends no email.
  - It runs with the identity migrator credentials.
  - There's no HTTP endpoint for it.
- Check: integration `test_bootstrap_owner.py`.

**PAY-101 Invite user (must).** An admin invites a user with `POST /v1/users/invite {email, name, roles[]}`. This creates a `pending` user and an invite, and queues the email. The email relay generates the token (32 random bytes, only its SHA-256 stored) when it sends. The token is valid for `INVITE_TTL_H` (default 72) from the send, and each send replaces it.
- Acceptance:
  - 201 with the user.
  - 409 `user_email_taken` for an existing email (case-insensitive).
  - 422 for an empty `roles` list or an unknown role.
  - 403 `forbidden_role` for a non-admin.
  - Audit `user.invited`.
- Check: unit and integration (identity); e2e `access.spec.ts`.

**PAY-102 Re-send invite (must).** `POST /v1/invites/{id}/resend` (admin) revokes the old token, issues a new one and queues a new email.
- Acceptance:
  - 200.
  - 409 `invite_not_pending` when the user is already active.
  - The old token then returns `invite_invalid`.
- Check: integration.

**PAY-103 Accept invite (must).** The invitee opens `/accept#token=<t>`; the page posts the token and a new password. The BFF calls `POST /v1/invites/accept`, which activates the user, stores the argon2id hash and marks the token used. The BFF then creates a session (PAY-106).
- Acceptance:
  - An expired, used or unknown token returns 400 `invite_invalid`.
  - A password shorter than 12 characters returns 422 `password_too_weak`.
  - The token never appears in a query string, log or span (PAY-NFR-07).
- Check: unit, integration, e2e.

**PAY-104 Sign in (must).** `POST /api/auth/login {email, password}` makes the BFF call identity `POST /v1/auth/login`. On success the BFF creates a session.
- Acceptance:
  - A wrong password or unknown email returns 401 `invalid_credentials`, with the same response shape. argon2 always runs, so the median latency of unknown-email and wrong-password logins differs by no more than 20% (integration `test_login_timing`).
  - An inactive user gets the same 401.
  - The limits are `AUTH_LOGIN_LIMIT_ACCOUNT` (5 / 15 min) and `AUTH_LOGIN_LIMIT_IP` (20 / 15 min). Past them the response is 429 `rate_limited` with `Retry-After`, and the account locks with exponential backoff (1 min doubling to 60 min).
  - Audit `auth.signin.succeeded` or `auth.signin.failed`, with `ip_hash` (PAY-NFR-08).
- Check: unit, integration (identity: `test_lockout_production_values`), e2e.

**PAY-105 Password reset (must).**
- `POST /v1/auth/password-reset/request {email}` always returns 202. For an active user it queues an email with a token valid for `RESET_TTL_H` (1).
- `POST /v1/auth/password-reset/confirm {token, password}` sets the password, bumps `roles_version`, and returns `{user_id, roles_version}`. The BFF then deletes every session in `web:user-sess:<user_id>`.
- As with invites, the relay generates the reset token at send time.
- Acceptance:
  - An invalid token returns 400 `reset_invalid`.
  - Reset requests are rate-limited like login.
  - After the reset, earlier sessions get 401 on their next request.
- Check: integration, e2e.

**PAY-106 Sessions (must).**
- **Storage:** sessions live in Redis at `web:sess:<HMAC-SHA256(SESSION_SECRET, 256-bit cookie id)>` holding `{user_id, cid, name, roles, roles_version, created_at, last_seen, roles_checked_at}`. The idle TTL is `SESSION_IDLE_TTL_S` (43200) and the absolute TTL `SESSION_ABS_TTL_S` (604800). Each session is also indexed in `web:user-sess:<user_id>`.
- **Cookie:** `sid` in dev and `__Host-sid` in prod; HttpOnly, Secure, SameSite=Lax.
- **Rotation:** a new ID at sign-in.
- **Sign-out** deletes the session.
- Acceptance:
  - Requests without a session cookie: pages redirect to `/login`, and `/api/*` returns 401 `unauthenticated`.
  - An expired session behaves the same as a missing one.
  - With Redis unavailable, session-protected routes return 503 `service_unavailable`.
- Check: Vitest (`session.test.ts`), e2e, resilience (Redis down).

**PAY-107 Role and status re-check (must).** `requireSession()` re-reads `GET /v1/users/me` when `roles_checked_at` is older than `SESSION_ROLE_RECHECK_S` (60). It ends the session when `roles_version` differs or the status isn't `active`.
- Acceptance:
  - While identity is reachable, a user demoted or deactivated at time T gets 401 (session ended) on any request after T + 60 s.
  - While identity is unreachable, the BFF keeps the cached roles for up to `SESSION_RECHECK_GRACE_S` (300 s) after the last successful check, then returns 503 `service_unavailable` (C37).
- Check: Vitest, e2e `role-change.spec.ts`.

**PAY-108 User administration (must).**
- `GET /v1/users` (admin; cursor, limit ≤ 100). Pending users include `pending_invite_id`.
- `PATCH /v1/users/{id} {roles?, status?}` (admin). Changing roles or deactivating bumps `roles_version`.
- Acceptance:
  - An admin can't change their own roles or status (409 `cannot_modify_self`).
  - Removing the last active admin returns 409 `last_admin`.
  - Audit `user.roles_changed` and `user.deactivated`.
- Check: integration, e2e.

**PAY-109 Auth audit log (must).** `GET /v1/audit-log` (admin; cursor) lists identity audit events, newest first: `{id, at, actor_user_id, event, target_user_id, ip_hash}`.
- Acceptance: it never contains passwords, tokens or raw IPs.
- Check: integration.

**PAY-110 Email delivery (must).**
- Identity queues emails in `email_outbox` (recipient, template, token reference). An in-process relay renders and sends them over SMTP through the resilient client.
- The relay retries with backoff up to `EMAIL_MAX_ATTEMPTS` (10), then marks the email `failed`.
- The rendered body is never stored.
- Acceptance:
  - With SMTP down, invite and reset still return 201/202, and the email is sent after recovery.
  - Mailpit receives the email in dev.
- Check: integration `test_email_relay.py`.

## 2. Company setup (J2): payroll service

**PAY-200 Company settings (must).**
- `GET /v1/company/settings` (any role) and `PUT /v1/company/settings` (admin).
- Fields: `name` (1–200), `tax_rate_bps` (0–5000), `retirement_bps` (0–2000), `timezone` (IANA, default `America/New_York`), `biweekly_anchor` (a date that is a Monday, default 2026-01-05), `require_dual_approval` (default true).
- `biweekly_anchor` can't change once any non-deleted biweekly run exists: 409 `anchor_locked`.
- Acceptance:
  - Out-of-range values return 422.
  - Every change writes a `settings.updated` audit event with the changed field names, old and new values (no personal data).
  - The defaults are seeded by a migration for `COMPANY_ID`.
- Check: unit, integration.

**PAY-201 Create employee (must).** `POST /v1/employees` (payroll_admin).
- **Fields:**
  - `name` (1–200), `email` (unique per company, case-insensitive);
  - `pay_type` (`salaried` with `annual_salary_cents` in 1–1,000,000,000, or `hourly` with `hourly_rate_cents` in 1–100,000);
  - `schedule` (`biweekly` | `monthly`);
  - `bank_routing` (9 digits, ABA checksum);
  - `bank_account` (4–17 digits).
- Acceptance:
  - 201 returns the employee with `account_last4` only.
  - 409 `employee_email_taken`.
  - 422 on validation errors, with field names in `error.details`.
  - Audit `employee.created`.
- Check: unit (validation, ABA checksum), integration, Schemathesis.

**PAY-202 List and read employees (must).**
- `GET /v1/employees?status=active|inactive&cursor=&limit=` (limit ≤ 100, default 50, ordered by name then id).
- `GET /v1/employees/{id}`.
- Acceptance:
  - The full routing and account numbers never appear in any response.
  - 404 `employee_not_found`, including for another company's employee.
- Check: integration, Schemathesis response check.

**PAY-203 Update employee (must).** `PATCH /v1/employees/{id}` (payroll_admin) updates any PAY-201 field, or `status: inactive` (sets `deactivated_at`).
- Acceptance:
  - Bank changes record `bank_updated_by` and `bank_updated_at`.
  - Audit `employee.updated`, with the changed field names only. For bank changes it records the old and new `account_last4`.
  - Reactivation clears `deactivated_at`.
- Check: integration.

**PAY-204 Bank details encrypted at rest (must).**
- `bank_routing_enc` and `bank_account_enc` are AES-256-GCM. The format is `kid:nonce:ciphertext` (base64), with the key taken from `PAYROLL_FIELD_KEYS[PAYROLL_FIELD_ACTIVE_KID]`.
- Decryption uses the stored kid.
- Acceptance:
  - A tampered ciphertext raises `FieldDecryptError`, which surfaces as 500 `internal_error` and log `crypto.decrypt_failed`.
  - A value encrypted with an old kid still decrypts after rotation.
- Check: unit `test_field_crypto.py`.

## 3. Prepare a pay run (J3): payroll service

**PAY-300 Create run (must).** `POST /v1/pay-runs {schedule, period_start}` (payroll_admin) creates a `draft` run.
- **Period rules** (plan §2.1):
  - monthly: `period_start` is the 1st, `period_end` is the month end, `period_index` is the month, and there are 12 periods per year;
  - biweekly: `period_start` = anchor + 14k days, `period_end` = start + 13 days, `period_index` per PAY-302, and there are 26 periods per year;
  - `pay_date` = `period_end`.
- **Snapshot:** each active employee on the schedule gets a `pending` payslip, snapshotting name, pay type and rate or salary.
- Acceptance:
  - 409 `pay_run_exists` for the same (company, schedule, period_start) unless that run was deleted.
  - 422 `invalid_period_start`.
  - 409 `no_employees`.
  - 409 `employee_in_overlapping_run` with the employee IDs, when an employee's days overlap another non-deleted run.
  - Audit `payrun.created`.
- Check: unit (periods), integration.

**PAY-301 Enter hours (must).** `PUT /v1/pay-runs/{id}/hours {entries: [{employee_id, hours_hundredths}]}` (payroll_admin) upserts hours for hourly payslips.
- Bounds: 0–33,600 biweekly and 0–74,400 monthly.
- Allowed in `draft` or `calculated`; a `calculated` run moves back to `draft`.
- Acceptance:
  - 409 `pay_run_locked` once the run is `processing` or final.
  - 422 `not_hourly` for a salaried employee.
  - 422 when out of bounds.
- Check: unit, integration.

**PAY-302 Pay calculation (must).** `POST /v1/pay-runs/{id}/calculate` (payroll_admin) computes each payslip in integer arithmetic (ADR 0006):
- **Salaried gross:** for period k of n, `base = annual // n`, `rem = annual % n`, gross = `base + (1 if k > n − rem else 0)`.
- **Biweekly k:** `((period_start − biweekly_anchor).days // 14) mod 26 + 1`.
- **Hourly gross:** `(rate_cents × hours_hundredths + 50) // 100`.
- **Tax:** `(gross × tax_bps + 5000) // 10000`.
- **Retirement:** `(gross × retirement_bps + 5000) // 10000`.
- **Net:** gross − tax − retirement.

The run snapshots `tax_bps`, `retirement_bps` and `require_dual_approval`, sets `calculated_by`, and moves to `calculated`.
- Acceptance:
  - 422 `hours_missing` with employee IDs when an hourly payslip has no hours.
  - 409 `pay_run_locked` after approval.
  - Run totals are sums of the stored payslip values.
  - Audit `payrun.calculated`.
- Check: unit hypothesis properties (PAY-NFR-01), integration.

**PAY-303 Run summary and payslips (must).**
- `GET /v1/pay-runs/{id}` returns status, period, `pay_date`, totals (`gross_cents`, `tax_cents`, `retirement_cents`, `net_cents`, `funding_required_cents` = Σ net), counts by payout status, `calculated_by`, `calculated_by_name`, `approved_by`, `approved_by_name`, `approved_at`, `processing_since`, `require_dual_approval`, and `counts.awaiting_bank` (indeterminate) and `counts.blocked` (needs_operator). The names come from the user token's `name` claim.
- `GET /v1/pay-runs?cursor=&limit=` lists runs, newest period first.
- `GET /v1/pay-runs/{id}/payslips` and `/payslips/{payslip_id}` return the payslip lines.
- Acceptance: for every payslip, gross = net + tax + retirement; `funding_required_cents` = Σ net.
- Check: integration, e2e `pay-run.spec.ts`.

**PAY-304 Delete run (must).** `DELETE /v1/pay-runs/{id}` (payroll_admin) soft-deletes a `draft` or `calculated` run (`deleted_at`). The period becomes free again.
- Acceptance: 409 `pay_run_locked` for other states. Audit `payrun.deleted`.
- Check: integration.

## 4. Approve and pay (J4): payroll, payout-worker, bank-stub

**PAY-400 Approve (must).** `POST /v1/pay-runs/{id}/approve` (approver; `Idempotency-Key` required) runs in one transaction:
- it locks the run and requires status `calculated`;
- it applies dual approval (PAY-401);
- it creates one payout per payslip with attempt 1 (`requested`, with an encrypted bank snapshot and `last_published_at`), or `paid` + `no_transfer` when net = 0;
- it writes a `payout.requested` outbox row per transfer;
- it sets `approved_by`, `approved_at` and status `processing` (or `paid` if every payout is no_transfer), and `processing_since`.
- Acceptance:
  - 200 with the run.
  - A repeat with the same key and body returns the stored response; the same key with a different body returns 422 `idempotency_key_reused`.
  - A second approve with a new key returns 409 `pay_run_not_approvable`.
  - Of two concurrent approves, exactly one succeeds.
  - 409 `employee_bank_details_missing` when an employee in the run has purged or missing bank details.
  - Audit `payrun.approved`.
- Check: unit, integration `test_approve_concurrency.py`.

**PAY-401 Dual approval (must).** When the run's `require_dual_approval` snapshot is true, the approver must differ from `calculated_by`, and from `bank_updated_by` of every employee in the run whose bank details changed after the run's `created_at`. When the company setting is turned off, runs already calculated keep their snapshot.
- Acceptance: 403 `approver_must_differ`.
- Check: unit, integration, e2e (two users).

**PAY-402 Outbox and stream (must).**
- payroll writes events to `payroll.outbox` in the same transaction as the state change.
- The relay (in every payroll replica: `FOR UPDATE SKIP LOCKED`, batch 100) runs `XADD stream:payout.requested MAXLEN ~ STREAM_MAXLEN` with the envelope `{event_id, type, version, occurred_at, producer, traceparent, data}`, then marks the row sent.
- `data` = `{payout_id, pay_run_id, company_id, attempt, amount_cents, currency: "USD", republish_seq}`, with no names and no bank details. `republish_seq` is 0 on the first publish.
- The stream entry has fields `event_id`, `type` and `envelope` (the JSON envelope). It's validated against `contracts/events/payout.requested.v1.json`.
- Acceptance: a rollback leaves neither the state change nor the event.
- Check: integration `test_outbox.py`, contract event schema test.

**PAY-403 Worker processing (must).** payout-worker (consumer group `payout-worker`; pending entries read first, then new ones) handles up to `PAYOUT_CONCURRENCY` (16) messages at once, per message:
1. Skip if the `event_id` is in `processed_events`.
2. Upsert `disbursements(payout_id, attempt)` to `submitting`.
3. Call `GET /v1/internal/payouts/{id}/instruction?attempt=n` on every delivery. A 409 `attempt_not_current` → mark done, record the event, ACK, with no bank call.
4. If the instruction's `amount_cents` differs from the event's → report `poison` (PAY-409) and dead-letter the message.
5. If the attempt is older than `BANK_KEY_SAFE_WINDOW_S` → `GET /v1/transfers/{key}` only (PAY-410).
6. Otherwise `POST /v1/transfers` with `Idempotency-Key = <payout_id>:<attempt>` and the instruction's amount and bank details.
7. Classify the response (PAY-404) and post the result (PAY-405).
8. In one transaction: disbursement `done`, insert into `processed_events`; then XACK.
- Acceptance: duplicate deliveries cause no second effect.
- Check: integration `test_consumer.py`.

**PAY-404 Bank answer classification (must).**
- 2xx with `status: settled` → `settled` (with `bank_reference`).
- 2xx with `status: rejected` and a `reason` → `rejected`.
- Anything else is "no answer": timeout, connection error, 5xx, 429, 409, 401/403, 404, 422, a malformed body, or an open breaker.
- "No answer" is retried within `BANK_TOTAL_DEADLINE_S` (15) and the delivery budget. Once `PAYOUT_MAX_DELIVERIES` (5) is reached, it's reported as `indeterminate`. It's never `rejected` or `failed`.
- Acceptance: one unit test per status class.
- Check: unit `test_classify.py`.

**PAY-405 Recording results (must).** `POST /v1/internal/payouts/{id}/result {attempt, outcome, reason?, bank_reference?}` (iss `payout-worker` only; `Idempotency-Key = <event_id>:<outcome>`). It locks the run, then:
- if the attempt isn't current → 409 `attempt_not_current`;
- if the current attempt is already final → 200 with the stored outcome, unchanged;
- `settled` → attempt settled, payout `paid`;
- `rejected` → attempt rejected (with reason), payout `failed`;
- `indeterminate` → attempt `indeterminate` (payout stays `requested`);
- `poison` → attempt `needs_operator`.

For an attempt in `needs_operator`, `settled` and `rejected` apply (the bank's answer wins), while `indeterminate` and `poison` return 200 with no change. The response body is `{payout_id, attempt, outcome}`, where `outcome` is the stored attempt status. Then the run status is recomputed (PAY-406).
- Acceptance: the outcomes above, plus audit `payout.result_recorded`.
- Check: unit, integration.

**PAY-406 Run status rule (must).** The status after approval is a pure function of its payouts:
- `processing` if any payout is `requested`;
- otherwise `paid` if all are `paid`;
- otherwise `partially_failed`.

`processing_since` is set on each entry into `processing`. The first time the run reaches a final status, `payroll_payout_settlement_seconds` is observed once, measured from `approved_at`.
- Acceptance: a property test shows the stored status always equals the function.
- Check: unit (hypothesis stateful), integration.

**PAY-407 Re-publish non-final attempts (must).** payroll's maintenance task (every `MAINTENANCE_INTERVAL_S`, 60; behind an advisory lock) re-publishes each attempt in `requested` or `indeterminate` when:
- `now − last_published_at` > `min(PAYOUT_REPUBLISH_AFTER_S × 2^(publish_count−1), PAYOUT_REPUBLISH_MAX_BACKOFF_S)` (360 s, capped at 900 s);
- and there's no unsent outbox row for that attempt.

It writes a new outbox row with a fresh `event_id`, increments `publish_count` and sets `republish_seq`. After `PAYOUT_REPUBLISH_MAX` (10) re-publishes, the attempt becomes `needs_operator`.
- Acceptance: after Redis FLUSHALL mid-run, the run still reaches a final status.
- Check: integration `test_republish.py`, resilience `test_redis_data_loss.py`.

**PAY-408 Worker failure handling (must).**
- XAUTOCLAIM reclaims messages idle for more than `PAYOUT_CLAIM_MIN_IDLE_MS` (60000).
- After `PAYOUT_MAX_DELIVERIES`:
  - if the last failure was the bank → report `indeterminate` and ACK;
  - if it was payroll (unreachable) or a worker DB failure → XADD to `stream:payout.requested.dlq` with the error, then ACK.
- **Poison goes to the DLQ at once,** not after N deliveries: a schema-invalid envelope, an amount mismatch, or `payout_not_found` on the instruction (PAY-409).
- On NOGROUP, the worker recreates the group with `XGROUP CREATE … 0 MKSTREAM`.
- On SIGTERM, it stops reading, finishes in-flight messages, and exits within the 75 s grace period.
- Acceptance: killing the worker mid-run, then restarting it, completes the run with no duplicate transfers.
- Check: integration, resilience `test_worker_crash.py`.

**PAY-409 Poison and needs_operator (must).** A schema-invalid message, or one whose amount doesn't match its instruction, is dead-lettered. When its payout ID is readable, payroll is told `poison`, so the attempt becomes `needs_operator`, re-publishing stops, and the UI shows "blocked".
- Acceptance: the alert `PayoutNeedsOperator` fires (PAY-NFR-11).
- Check: integration.

**PAY-410 Key safe window (must).** For an attempt whose `requested_at` is older than `BANK_KEY_SAFE_WINDOW_S` (86400), the worker only calls `GET /v1/transfers/{key}`. If found, that transfer's status is the answer. A 404 makes the worker report `poison` with reason `key_window_expired`, so the attempt becomes `needs_operator`. It never POSTs.
- Acceptance: with a 25-hour-old attempt and no transfer at the bank, the bank receives no POST and the attempt ends `needs_operator`; with a transfer present, its status is recorded.
- Check: unit, integration.

**PAY-411 Internal instruction endpoint (must).** `GET /v1/internal/payouts/{id}/instruction?attempt=n` (iss `payout-worker`, sub `service:payout-worker`, and the payout must belong to the token's `cid`) returns `{payout_id, attempt, amount_cents, currency, routing, account, requested_at}`. Routing and account are decrypted from the attempt's snapshot.
- Acceptance:
  - 409 `attempt_not_current` unless attempt n is current and `requested` or `indeterminate`.
  - 401 for any other issuer.
  - 404 `payout_not_found` for another company.
- Check: integration, unit (auth).

**PAY-412 bank-stub (must).**
- `POST /v1/transfers {amount_cents, currency, routing, account}` requires an `Idempotency-Key` header (`^[A-Za-z0-9:_-]{1,255}$`) and `X-Api-Key: BANK_STUB_API_KEY`.
  - It does `INSERT … ON CONFLICT (idempotency_key) DO NOTHING RETURNING` into `bank_stub.transfers`.
  - The response is `{transfer_id, status, reason, bank_reference}`: 201 for a new transfer.
  - A repeated key returns the stored row (200), whatever the body. The key lookup happens before body validation.
- **Outcome:** an account ending `0000` → `rejected` / `account_closed`. Otherwise `rejected` / `insufficient_funds_at_bank` when `sha256(key)` mod 10000 < `BANK_STUB_FAILURE_RATE` × 10000; else `settled` with a `bank_reference`.
- **Latency:** `BANK_STUB_LATENCY_MS` (20).
- `GET /v1/transfers/{key}` returns 200 or 404.
- Acceptance:
  - A missing API key returns 401.
  - Concurrent POSTs with one key create one row.
  - The ledger survives a restart.
- Check: unit, integration.

**PAY-413 Live progress (must).** The run page polls `GET /api/pay-runs/{id}` every 2 s while the status is `processing`, showing counts by payout status and, per payout, `awaiting bank` (indeterminate) or `blocked` (needs_operator).
- Acceptance: polling stops once the run is final; the `counts` from PAY-303 are shown; the badges appear for the matching attempt statuses.
- Check: Vitest, e2e.

## 5. Recover failed payouts (J5)

**PAY-500 Retry payout (must).** `POST /v1/pay-runs/{id}/payouts/{payout_id}/retry` (approver) runs in one transaction:
- it requires the payout to be `failed` (current attempt `rejected`);
- it requires `current_attempt` < `PAYOUT_MAX_ATTEMPTS` (5, including the first);
- it requires the employee to be active with bank details;
- under dual approval, it requires the retrier ≠ the employee's `bank_updated_by`;
- it creates attempt n+1 with a new bank snapshot, sets the payout to `requested`, writes the outbox row, and recomputes the run status (back to `processing`).
- Acceptance:
  - 409 `payout_not_retryable` (including indeterminate and needs_operator attempts).
  - 409 `retry_limit_reached`.
  - 409 `employee_bank_details_missing`.
  - 403 `approver_must_differ`.
  - Audit `payout.retry_requested`.
- Check: unit, integration, e2e `recover.spec.ts`.

**PAY-501 Payout list with history (must).** `GET /v1/pay-runs/{id}/payouts` returns each payout with its `payslip_id`, employee name, `amount_cents`, status, `no_transfer` and attempts `[{attempt, status, failure_reason, bank_reference, account_last4, requested_at, final_at}]`.
- Acceptance: never any account number other than `account_last4`.
- Check: integration, e2e.

**PAY-502 Operator resolution (must).** `POST /v1/pay-runs/{id}/payouts/{payout_id}/resolve {action, note}` (approver; `note` is 1–500 characters) applies only to a payout whose current attempt is `needs_operator`:
- `redrive` sets the attempt back to `requested`, sets `publish_count` to 1 (with `republish_seq` 0 and `last_published_at` now), and writes a new outbox row. Use it after the cause is fixed, for example payroll is back or the DLQ has been inspected.
- `mark_rejected` sets the attempt to `rejected` (`failure_reason = "operator_rejected"`) and the payout to `failed`, so the normal retry (PAY-500) opens. It's refused with 409 `resolve_too_early` (and `Retry-After`) until `PAYOUT_MARK_REJECTED_MIN_WAIT_S` (120 s) after the attempt entered `needs_operator`. That's longer than the worker's bound on in-flight bank POSTs (`HANDLER_DEADLINE_S`, 35 s), so no earlier POST can still settle (C48). The runbook `PayoutNeedsOperator.md` requires checking the bank first (`GET /v1/transfers/{key}` shows no settled transfer).
- Either action recomputes the run status. Audit `payout.operator_resolved`, with the action and the note's length (not its text).
- Acceptance:
  - 409 `payout_not_blocked` when the attempt isn't `needs_operator`;
  - 409 `resolve_too_early` with `Retry-After` for `mark_rejected` within `PAYOUT_MARK_REJECTED_MIN_WAIT_S` of entering `needs_operator`; `redrive` succeeds at once;
  - 422 `validation_error` for an unknown action or a missing note.
- Check: unit, integration, e2e (`recover.spec.ts` blocked case).

## 6. Cross-cutting

**PAY-600 Internal JWT (must).**
- The BFF signs EdDSA tokens with `iss=web`, `aud` set to the service, `sub` = the user ID, `name`, `roles`, `cid`, `sid`, `iat`, `exp` (60 s) and `jti`.
- For public flows, the BFF signs `sub=service:web` with `cid = COMPANY_ID`, `roles: []`, and no `name` or `sid`.
- The worker signs `iss=payout-worker`, `sub=service:payout-worker`, `cid` from the event, `roles: []`, `jti`, and no `sid`.
- Each service verifies against its keyset `INTERNAL_JWT_PUBLIC_KEYS` `{kid: {iss, pem}}`. The token's `iss` must equal the kid's bound issuer. 5 s leeway.
- Acceptance: 401 `invalid_token` when the token is missing, expired, has the wrong aud, an unknown kid, a tampered signature, or an issuer that doesn't match its kid.
- Check: pycommon unit `test_jwt.py`.

**PAY-601 Authorization (must).** Services enforce the role matrix in plan §8 in every handler, and scope every query by `cid`. The BFF re-checks the session and roles in every route handler and server component; the request interceptor only checks that a cookie is present.
- Acceptance: 403 `forbidden_role`.
- Check: unit per router (parametrised matrix), Vitest.

**PAY-602 Error format (must).** Every service and the BFF return errors as `{error: {code, message, request_id, details?}}`.
- Validation errors are 422 `validation_error`, with `details` `[{field, issue}]` (never the submitted value). `field` is the request location joined by dots: `body.bank_routing`, `body.entries.3.hours_hundredths`, `query.limit`, `header.Idempotency-Key` (C56).
- Not-found errors use `<entity>_not_found`: `pay_run_not_found`, `payslip_not_found`, `payout_not_found`, `employee_not_found`, `user_not_found`, `invite_not_found`, `company_settings_not_found`.
- Other added codes: `employee_not_in_run`, `idempotency_key_required`, `anchor_locked`, `payout_not_blocked`, `resolve_too_early`; BFF only, `invalid_json` (400) and `request_failed` (fallback).
- List responses are `{items, next_cursor}`, where `next_cursor` is an opaque base64url string or null.
- Unhandled errors are 500 `internal_error`, with no traceback in the response.
- The BFF maps service errors to user-safe messages and never forwards internals.
- Check: pycommon unit, Vitest.

**PAY-603 Rate limits (must).** These are backed by Redis:
- BFF per session `BFF_RATE_LIMIT_SESSION` (120/min);
- BFF per IP `BFF_RATE_LIMIT_IP` (300/min), with the IP taken from `TRUSTED_PROXY_HOPS`;
- services per `(iss, sub)`: `SERVICE_RATE_LIMIT_USER` (600/min) and `SERVICE_RATE_LIMIT_WORKER` (6000/min). `service:web` is exempt; identity's public flows are limited per IP instead (accept and reset-confirm use `AUTH_LOGIN_LIMIT_IP`).
- The BFF sends `X-Client-IP`. Services trust it only on `iss=web` tokens, and use it for per-IP limits and `ip_hash`.

When Redis is unavailable, login, reset and invite-accept fail closed (503 `service_unavailable`), and the other limits fail open (`rate_limiter_errors_total`).
- Acceptance: 429 `rate_limited` with `Retry-After`.
- Check: integration (two app instances share counts), Vitest.

**PAY-604 BFF protections (must).**
- An `Origin` check on every mutation (403 `bad_origin`).
- Body limits of 64 KB at the BFF and 256 KB at services (413 `payload_too_large`).
- Headers: nonce-based CSP (ADR 0009), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and HSTS in prod.
- Every BFF response carries `x-request-id` and `x-trace-id`.
- Check: Vitest, e2e (headers, no CSP violations).

**PAY-605 Health and startup (must).**
- `/livez` and `/readyz` on every service. `/readyz` checks the DB, Redis (not for bank-stub, which has no Redis) and that migrations are at head, and fails when any is down.
- Startup checks in uat and prod refuse to start when:
  - a required secret is missing or a dev default;
  - a key ID starts with `dev-` or `test-`;
  - debug or docs are on;
  - `BANK_BASE_URL` points at bank-stub or a placeholder (prod);
  - the timing invariants on `PAYOUT_CLAIM_MIN_IDLE_MS` and `PAYOUT_REPUBLISH_AFTER_S` fail (plan §2.2).

  They log `startup.check_failed` with the failing setting's name (never its value), and exit 2.
- Acceptance: each listed condition, set in a uat-like env, stops start-up with exit 2; `/readyz` returns 503 within 5 s of the DB or Redis going down.
- Check: pycommon unit `test_startup_checks.py`, integration (`test_readyz`).

**PAY-606 Data ownership (must).** The schemas are `identity`, `payroll`, `payouts` and `bank_stub`, each with a migrator role and a runtime role. Neither role has rights in another schema. Bootstrap SQL in `deploy/compose/init/` is shared by Compose, Testcontainers and the Kubernetes Job.
- Acceptance: a runtime role querying another schema gets `permission denied`.
- Check: integration `test_grants.py` per service.

**PAY-607 Redis ACLs (must).** Each service connects as its own ACL user (plan §6). Acceptance: web can't XADD to `stream:payout.requested`. Check: integration.

**PAY-608 Maintenance and retention (must).** Each service runs its maintenance task behind an advisory lock and purges by:
- `RETENTION_OUTBOX_DAYS` (7);
- `RETENTION_PROCESSED_EVENTS_DAYS` (30);
- `RETENTION_TOKENS_DAYS` (7 after expiry);
- `IDEMPOTENCY_TTL_H` (24);
- `RETENTION_BANK_DETAILS_DAYS` (30 after deactivation): employee bank fields and the final attempts' snapshots are nulled when the employee has no non-final attempt;
- emails 7 days after they're sent or failed.
- Acceptance: rows past each window are gone after one maintenance tick, younger rows remain, and two replicas never run the same tick (advisory lock).
- Check: integration `test_maintenance.py`.

**PAY-609 Seed data (must).** `scripts/seed.py` (dev and CI only; it refuses to run when `ENV` isn't dev) works through the APIs and Mailpit:
- it creates `approver@payrun.local` (approver) and `LOAD_USER_EMAIL` (payroll_admin) via invite and accept;
- it sets the company settings (tax 2000 bps, retirement 500 bps; the migration seeds 0/0);
- it creates employees: by default 10 (salaried and hourly, one with an account ending `0000`), or 500 with `--employees 500`;
- `--skeleton` (S8) creates only 5 salaried monthly employees, one calculated monthly run and one draft run, and makes no settings PUT;
- it prints a JSON summary (user IDs, employee count, run IDs) that k6 and e2e read;
- `scripts/mint_token.py` (CLI and importable `mint()`) and the Schemathesis hook `scripts/ci/schemathesis_auth.py` mint a fresh token per request, since tokens expire in 60 s.

The `bootstrap-owner` Compose one-shot runs `bootstrap_owner --if-empty --password-env SEED_OWNER_PASSWORD`, so the dev owner is active, and seed signs in as it.
- Acceptance: a re-run is a no-op (same counts); it exits 2 when `ENV` isn't dev; the JSON summary validates.
- Check: `tests/platform/test_seed.py` against the stack, plus the e2e global setup.

## 7. Non-functional

**PAY-NFR-01 Money correctness (must).** Hypothesis property tests over random salaries, rates, hours and periods prove:
- (a) per payslip, gross = net + tax + retirement, and net ≥ 0;
- (b) Σ salaried gross over periods 1..n = annual;
- (c) the run funding total = Σ net.

No `float` in `payroll.domain`; a lint test bans it.
- Check: unit `test_calc_properties.py`, `test_no_float.py`.

**PAY-NFR-02 No double payment (must).** Per `payout_id`: at most one settled bank transfer across all attempts; `paid` ⇔ (`no_transfer` and zero transfers) or exactly one settled transfer. This holds under:
- redelivery;
- a crash at each step;
- two consumers;
- re-publish racing a live consumer;
- a late or reordered result;
- a bank timeout after settling;
- a bank-stub restart.
- Check: unit (hypothesis stateful model `test_payout_model.py`), integration (§12.1 rows), resilience (worker kill, bank restart, timeout toxic).

**PAY-NFR-03 Liveness (must).** With a healthy bank, every payout of an approved run reaches a final status. With an unhealthy bank, attempts stay non-final and are re-driven (PAY-407) until the bank answers, or are surfaced as `needs_operator`.
- Check: resilience (bank down then up; Redis FLUSHALL; payroll down then up).

**PAY-NFR-04 Availability SLO (must).** 99.5% of BFF requests are not 5xx, measured monthly. Burn-rate alerts: page at 14.4× over 1 h and 5 m; ticket at 6× over 6 h and 30 m; budget 0.005.
- Check: `promtool test rules`.

**PAY-NFR-05 Latency SLO (must).** BFF p95 < 300 ms for reads and simple writes, and p95 < 1.5 s for calculate and approve on 500 employees.
- PR k6 smoke: 5 VUs × 30 s, p95 < 300 ms, errors < 1%.
- Nightly: 30 req/s for 10 min, plus a soak at 5 req/s for 30 min, with no memory growth (RSS slope < 5% over the soak) and no consumer-lag growth.
- Check: `load/smoke.js`, `load/full.js`.

**PAY-NFR-06 Settlement SLO (must).** A 500-employee run on the healthy-bank path has all payouts final within 60 s of approval at p95.
- Check: `load/settlement.js` (nightly), observed in `payroll_payout_settlement_seconds`.

**PAY-NFR-07 No secrets or sensitive data in telemetry (must).** Logs, spans and metric labels never contain:
- passwords, tokens (invite, reset, JWT, session), cookies or `Authorization` headers;
- API keys;
- full bank numbers;
- salaries or amounts tied to a person;
- user-entered text outside dev (lengths only).

A deny-list redactor in pycommon and the BFF masks keys named `password`, `token`, `authorization`, `cookie`, `account`, `routing` and `api_key`.
- Check: unit (redactor); integration (a secret set in settings never appears in captured logs); e2e (Loki and Tempo searched for the invite token after J1).

**PAY-NFR-08 Privacy (must).**
- Personal data is limited to the plan §6 table, with those retention periods.
- `ip_hash` = HMAC-SHA256(`AUDIT_IP_HASH_KEY`, ip).
- The API returns only the last 4 digits of bank numbers.
- Check: unit and integration (PAY-608, PAY-202).

**PAY-NFR-09 Tracing (must).** A J4 journey is one trace: web → payroll → (stream) → payout-worker → payroll and bank-stub. Every log line carries `trace_id`; Loki has at least one line per service for that trace.
- Check: e2e `trace.spec.ts`.

**PAY-NFR-10 Observability assets (must).**
- **Metrics:** those in plan §11 and `observability.md` §5, with the names exact.
- **Dashboards:** Service overview, Journeys, Queues and workers, Bank dependency, Platform.
- **Alerts:** the plan §11 table, each with `docs/runbooks/<AlertName>.md`.
- **Routing:** Alertmanager by severity to the webhook sinks.
- Check: `promtool check` and `promtool test rules`, `amtool check-config`, an in-memory metric test per business metric, the Step 6 drills.

**PAY-NFR-11 Paging alerts for stuck money (must).** `PayoutDLQ`, `PayoutsStuck`, `PayoutNeedsOperator` and `PayoutSettlementSlow` fire on their plan §11 expressions and stay quiet otherwise.
- Check: `promtool test rules` fixtures.

**PAY-NFR-12 Quality gates (must).**
- **Python:** ruff, ruff format, `mypy --strict`, and coverage ≥ 85% per service, ≥ 90% for pycommon.
- **Web:** ESLint, Prettier, `tsc`, Vitest coverage ≥ 80%.
- **Contracts:**
  - the OpenAPI and TypeScript type drift check shows no diff;
  - oasdiff finds no breaking change (skipped when `main` has no spec);
  - Schemathesis finds no 5xx and no schema violations;
  - events validate against their schemas.
- **e2e** passes, including the trace and axe tests (no serious or critical violations).
- **Images:** Trivy finds no HIGH or CRITICAL CVE.
- **Audits:** pip-audit and pnpm audit high.
- **Kubernetes:** kubeconform passes for every overlay.
- **Secrets:** gitleaks finds nothing.
- **Alert rules:** `promtool check rules`, `promtool test rules` and `amtool check-config` pass.
- **Fault smoke tests:** 1–2 Toxiproxy tests in the PR e2e job (slow DB, identity down).
- **Trivy ignores:** a `.trivyignore` entry needs a reason, an owner and `exp:YYYY-MM-DD`, also recorded in `SECURITY_DEPENDENCIES.md`.
- **Nightly:** a failed nightly run opens or updates a GitHub issue with the failing step's log.
- Check: CI `ci.yml` jobs; recorded in `docs/TESTING.md`.

**PAY-NFR-13 Deployability (must).**
- **Compose:**
  - profiles `core`, `obs` and `test`;
  - the `compose.test.yaml` override;
  - every port bound to 127.0.0.1 at `APP_PORT_BASE` (4100) offsets;
  - `scripts/stack.sh up` returns healthy.
- **Kubernetes:**
  - a Kustomize base with dev, uat and prod overlays;
  - non-root, read-only root filesystem, all capabilities dropped;
  - default-deny NetworkPolicies with the plan §13 allows;
  - versioned migration Jobs and a db-bootstrap Job.
- **Images** are built for `linux/amd64` in CI.
- Check: the `manifests` and `e2e` CI jobs.

**PAY-NFR-15 Recovery and startup (must).**
- Recovery point objective 5 min (managed Postgres point-in-time recovery in prod) and recovery time objective 1 h.
- Redis AOF means queued payouts survive a restart.
- Each service reports ready within 20 s after its migrations finish.
- Acceptance: in Compose, restarting Redis mid-run loses no payout (the run completes); each service's `/readyz` turns 200 within 20 s of its container start, once the migrations are done.
- Check: resilience `test_redis_restart.py`; e2e start-up timing recorded by `stack.sh up`.

**PAY-NFR-14 Accessibility (should).** The main pages (login, dashboard, employees, run detail, payouts) have no serious or critical axe violations.
- Check: e2e with `@axe-core/playwright`.
