# PayRun API reference (index)

**The source of truth is each service's `openapi.json`:**
[`services/identity/openapi.json`](../services/identity/openapi.json),
[`services/payroll/openapi.json`](../services/payroll/openapi.json) and
[`services/bank_stub/openapi.json`](../services/bank_stub/openapi.json). They are generated from
the code by `uv run python scripts/export_openapi.py --all`, and CI fails if a committed file
drifts (`scripts/ci/contract.sh`). Request and response schemas, every status code and every
parameter are there; this page is an index with the auth rules the specs don't carry. payout-worker
has no API beyond `/livez` and `/readyz`. Field-level contracts are in each low-level design (§4).

The browser never calls a service. It calls the BFF (`/api/*` on the web app), which calls the
services with internal JWTs.

## Authentication between services

| Caller → service        | Credential                                                                                              |
| ----------------------- | ------------------------------------------------------------------------------------------------------- |
| web → identity          | `Authorization: Bearer <JWT>`, EdDSA, `iss=web`, `aud=identity`, 60 s; `sub` is the user ID, or `service:web` for the public flows |
| web → payroll           | `Authorization: Bearer <JWT>`, `iss=web`, `aud=payroll`; user tokens only                               |
| payout-worker → payroll | `Authorization: Bearer <JWT>`, `iss=payout-worker`, `sub=service:payout-worker`, `aud=payroll`, `roles: []`; `/v1/internal/*` only |
| payout-worker → bank-stub | `X-Api-Key: <BANK_STUB_API_KEY>`                                                                       |

Each service verifies against its own issuer-bound keyset (`INTERNAL_JWT_PUBLIC_KEYS`, spec C30):
a missing, expired or mis-issued token is 401 `invalid_token`; a valid token of the wrong kind or
without the role is 403 `forbidden_role`. Roles are additive; "any role" means `admin`,
`payroll_admin` or `approver`. The BFF sends `X-Client-IP`, which services trust only on
`iss=web` tokens. For manual dev calls, mint a token with `scripts/mint_token.py`
([RUNBOOK](RUNBOOK.md#mint-a-token-for-manual-api-calls-dev-only)).

## identity

Base path `/v1`; container port 8000 (dev host port 4110 with the test profile). Audience
`identity`; accepts `iss=web` only. Design: [identity LLD](specs/payrun-0.1/design-low-level-identity.md).

| Method | Path                              | Summary                                                    | Roles / caller |
| ------ | --------------------------------- | ---------------------------------------------------------- | -------------- |
| GET    | `/v1/audit-log`                   | The auth audit log, newest first                           | admin          |
| POST   | `/v1/auth/login`                  | Sign in with email and password                            | `service:web`  |
| POST   | `/v1/auth/password-reset/confirm` | Set a new password with a reset token                      | `service:web`  |
| POST   | `/v1/auth/password-reset/request` | Request a password reset email; always 202                 | `service:web`  |
| POST   | `/v1/invites/accept`              | Accept an invite and set a password                        | `service:web`  |
| POST   | `/v1/invites/{id}/resend`         | Revoke the user's pending invite and send a new one        | admin          |
| GET    | `/v1/users`                       | Users in creation order                                    | admin          |
| POST   | `/v1/users/invite`                | Invite a user                                              | admin          |
| GET    | `/v1/users/me`                    | The calling user, whatever its status                      | any user token |
| PATCH  | `/v1/users/{id}`                  | Change a user's roles or deactivate them (not on yourself) | admin          |

## payroll

Base path `/v1`; container port 8000 (dev host port 4111). Audience `payroll`; public routes
accept `iss=web` user tokens, `/v1/internal/*` only the worker. Design and the event contract:
[payroll LLD](specs/payrun-0.1/design-low-level-payroll.md).

| Method | Path                                                 | Summary                                         | Roles / caller |
| ------ | ---------------------------------------------------- | ----------------------------------------------- | -------------- |
| GET    | `/v1/audit-events`                                   | List payroll audit events                       | admin          |
| GET    | `/v1/company/settings`                               | Get company settings                            | any role       |
| PUT    | `/v1/company/settings`                               | Put company settings                            | admin          |
| GET    | `/v1/employees`                                      | List employees                                  | any role       |
| POST   | `/v1/employees`                                      | Create an employee                              | payroll_admin  |
| GET    | `/v1/employees/{employee_id}`                        | Get an employee                                 | any role       |
| PATCH  | `/v1/employees/{employee_id}`                        | Update, deactivate or reactivate an employee    | payroll_admin  |
| GET    | `/v1/pay-runs`                                       | List pay runs                                   | any role       |
| POST   | `/v1/pay-runs`                                       | Create a pay run                                | payroll_admin  |
| GET    | `/v1/pay-runs/{run_id}`                              | Get a pay run (with counts)                     | any role       |
| DELETE | `/v1/pay-runs/{run_id}`                              | Delete a draft or calculated run                | payroll_admin  |
| PUT    | `/v1/pay-runs/{run_id}/hours`                        | Put hours for hourly employees                  | payroll_admin  |
| POST   | `/v1/pay-runs/{run_id}/calculate`                    | Calculate the run                               | payroll_admin  |
| POST   | `/v1/pay-runs/{run_id}/approve`                      | Approve; `Idempotency-Key` required             | approver       |
| GET    | `/v1/pay-runs/{run_id}/payslips`                     | List payslips                                   | any role       |
| GET    | `/v1/pay-runs/{run_id}/payslips/{payslip_id}`        | Get a payslip                                   | any role       |
| GET    | `/v1/pay-runs/{run_id}/payouts`                      | List payouts with their attempts                | any role       |
| POST   | `/v1/pay-runs/{run_id}/payouts/{payout_id}/retry`    | Retry a failed payout; `Idempotency-Key` optional | approver     |
| POST   | `/v1/pay-runs/{run_id}/payouts/{payout_id}/resolve`  | Resolve a `needs_operator` attempt (`redrive` or `mark_rejected`) | approver |
| GET    | `/v1/internal/payouts/{payout_id}/instruction`       | The current attempt's instruction, with decrypted bank details | payout-worker |
| POST   | `/v1/internal/payouts/{payout_id}/result`            | Record a bank result; `Idempotency-Key: <event_id>:<outcome>` | payout-worker |

## bank-stub

Base path `/v1`; container port 8000 (dev host port 4113). Not a JWT service: every call needs
`X-Api-Key`; the only caller is payout-worker. Deployed in dev and uat only. Design:
[bank-stub LLD](specs/payrun-0.1/design-low-level-bank-stub.md).

| Method | Path                  | Summary                                                                                    | Caller        |
| ------ | --------------------- | ------------------------------------------------------------------------------------------ | ------------- |
| POST   | `/v1/transfers`       | Create a transfer; `Idempotency-Key` required; 201 new, 200 replay of the same key (spec R6) | payout-worker |
| GET    | `/v1/transfers/{key}` | Look a transfer up by its idempotency key; 404 `transfer_not_found`                        | payout-worker, operators |

## Health endpoints

Every Python service serves `GET /livez` and `GET /readyz` (not in `openapi.json`); the web app
serves `GET /api/livez` and `GET /api/readyz`. See [RUNBOOK](RUNBOOK.md#health-and-readiness).

## BFF routes (`apps/web/src/app/api/**/route.ts`)

The browser API. Each handler declares its auth in `route({ auth: ... })`: `public` (no session),
`optional`, `{}` (any signed-in user) or `{ roles: [...] }`. Every mutation checks the Origin.
Any other method on a listed path returns 405 `method_not_allowed` with `Allow`; an unknown
`/api/*` path returns 404 (`api/[...path]/route.ts`). `[id]` is a pay run, employee, invite or user
ID as the path suggests.

| Method | Path                                          | Auth                     | Upstream                                                   |
| ------ | --------------------------------------------- | ------------------------ | ---------------------------------------------------------- |
| GET    | `/api/livez`                                  | public                   | —                                                          |
| GET    | `/api/readyz`                                 | public                   | —                                                          |
| POST   | `/api/auth/login`                             | public (IP limit, fails closed) | identity `POST /v1/auth/login`                      |
| POST   | `/api/auth/logout`                            | optional                 | — (deletes the session)                                    |
| GET    | `/api/auth/me`                                | any signed-in user       | identity `GET /v1/users/me` (forced re-check)              |
| POST   | `/api/auth/accept`                            | public (IP limit, fails closed) | identity `POST /v1/invites/accept`                  |
| POST   | `/api/auth/reset/request`                     | public (IP limit, fails closed) | identity `POST /v1/auth/password-reset/request`     |
| POST   | `/api/auth/reset/confirm`                     | public (IP limit, fails closed) | identity `POST /v1/auth/password-reset/confirm`     |
| GET    | `/api/users`                                  | admin                    | identity `GET /v1/users`                                   |
| POST   | `/api/users/invite`                           | admin                    | identity `POST /v1/users/invite`                           |
| PATCH  | `/api/users/[id]`                             | admin                    | identity `PATCH /v1/users/{id}`                            |
| POST   | `/api/invites/[id]/resend`                    | admin                    | identity `POST /v1/invites/{id}/resend`                    |
| GET    | `/api/audit/auth`                             | admin                    | identity `GET /v1/audit-log`                               |
| GET    | `/api/audit/payroll`                          | admin                    | payroll `GET /v1/audit-events`                             |
| GET    | `/api/settings`                               | any signed-in user       | payroll `GET /v1/company/settings`                         |
| PUT    | `/api/settings`                               | admin                    | payroll `PUT /v1/company/settings`                         |
| GET    | `/api/employees`                              | any signed-in user       | payroll `GET /v1/employees`                                |
| POST   | `/api/employees`                              | payroll_admin            | payroll `POST /v1/employees`                               |
| GET    | `/api/employees/[id]`                         | any signed-in user       | payroll `GET /v1/employees/{id}`                           |
| PATCH  | `/api/employees/[id]`                         | payroll_admin            | payroll `PATCH /v1/employees/{id}`                         |
| GET    | `/api/pay-runs`                               | any signed-in user       | payroll `GET /v1/pay-runs`                                 |
| POST   | `/api/pay-runs`                               | payroll_admin            | payroll `POST /v1/pay-runs`                                |
| GET    | `/api/pay-runs/[id]`                          | any signed-in user       | payroll `GET /v1/pay-runs/{id}`                            |
| DELETE | `/api/pay-runs/[id]`                          | payroll_admin            | payroll `DELETE /v1/pay-runs/{id}`                         |
| PUT    | `/api/pay-runs/[id]/hours`                    | payroll_admin            | payroll `PUT /v1/pay-runs/{id}/hours`                      |
| POST   | `/api/pay-runs/[id]/calculate`                | payroll_admin            | payroll `POST /v1/pay-runs/{id}/calculate`                 |
| POST   | `/api/pay-runs/[id]/approve`                  | approver                 | payroll `POST /v1/pay-runs/{id}/approve`                   |
| GET    | `/api/pay-runs/[id]/payslips`                 | any signed-in user       | payroll `GET /v1/pay-runs/{id}/payslips`                   |
| GET    | `/api/pay-runs/[id]/payslips/[payslipId]`     | any signed-in user       | payroll `GET /v1/pay-runs/{id}/payslips/{payslip_id}`      |
| GET    | `/api/pay-runs/[id]/payouts`                  | any signed-in user       | payroll `GET /v1/pay-runs/{id}/payouts`                    |
| POST   | `/api/pay-runs/[id]/payouts/[payoutId]/retry` | approver                 | payroll `POST …/payouts/{payout_id}/retry`                 |
| POST   | `/api/pay-runs/[id]/payouts/[payoutId]/resolve` | approver               | payroll `POST …/payouts/{payout_id}/resolve`               |
| POST   | `/api/telemetry`                              | optional                 | — (browser telemetry, own IP limit)                        |

Lists everywhere are `{items: [...], next_cursor: string | null}` with an opaque base64url cursor;
`limit` ≤ 100, default 50 (spec R4, C8).

## Errors

Every service and the BFF return errors as (PAY-602):

```json
{ "error": { "code": "validation_error", "message": "...", "request_id": "...", "details": [{ "field": "body.entries.3.hours_hundredths", "issue": "..." }] } }
```

- `details` for `validation_error` is `[{field, issue}]`, where `field` is the request location
  joined by dots (`body.<path>`, `query.<name>`, `header.<Name>`); the submitted value is never
  echoed (spec C14, C56). Some codes carry `details.employee_ids` instead (spec C15).
- Unhandled errors are 500 `internal_error` with no traceback. 429 responses carry `Retry-After`.
- The BFF maps service errors to user-safe messages through its catalogue,
  [`apps/web/src/lib/errors.ts`](../apps/web/src/lib/errors.ts), and never forwards internals.

| Status | Codes                                                                                                   |
| ------ | ------------------------------------------------------------------------------------------------------- |
| 400    | `bad_request`, `idempotency_key_required`, `invite_invalid`, `reset_invalid`; BFF only: `invalid_json`  |
| 401    | services: `invalid_token`, `invalid_credentials`, bank-stub `invalid_api_key`; BFF: `unauthenticated`   |
| 403    | `forbidden_role`, `approver_must_differ`; BFF only: `bad_origin`                                        |
| 404    | `not_found`, `user_not_found`, `invite_not_found`, `company_settings_not_found`, `employee_not_found`, `pay_run_not_found`, `payslip_not_found`, `payout_not_found`, bank-stub `transfer_not_found` |
| 405    | `method_not_allowed`                                                                                    |
| 409    | `conflict`, `user_email_taken`, `invite_not_pending`, `cannot_modify_self`, `last_admin`, `anchor_locked`, `employee_email_taken`, `pay_run_exists`, `no_employees`, `employee_in_overlapping_run`, `pay_run_locked`, `pay_run_not_approvable`, `payout_not_retryable`, `retry_limit_reached`, `employee_bank_details_missing`, `payout_not_blocked`, `resolve_too_early`, `attempt_not_current` (internal) |
| 413    | `payload_too_large`                                                                                     |
| 415    | BFF: `unsupported_media_type`                                                                           |
| 422    | `validation_error`, `password_too_weak`, `invalid_period_start`, `not_hourly`, `employee_not_in_run`, `hours_missing`, `idempotency_key_reused` |
| 429    | `rate_limited`                                                                                          |
| 500    | `internal_error`                                                                                        |
| 503    | `service_unavailable`, `db_unavailable`, `db_timeout`                                                   |

The authoritative list per route is in each `openapi.json` and the LLD route tables; the code
lists are PAY-602 and spec R3, C15.

## Event contracts (`contracts/events/`)

| Schema                                                                              | Stream                        | Producer → consumer                   |
| ----------------------------------------------------------------------------------- | ----------------------------- | ------------------------------------- |
| [`payout.requested.v1.json`](../contracts/events/payout.requested.v1.json)          | `stream:payout.requested`     | payroll outbox relay → payout-worker (group `payout-worker`) |
| [`payout.requested.dlq.v1.json`](../contracts/events/payout.requested.dlq.v1.json)  | `stream:payout.requested.dlq` | payout-worker → operators             |

- **`payout.requested` v1** envelope: `event_id`, `type`, `version`, `occurred_at`, `producer`,
  `traceparent`, `data`. `data`: `payout_id`, `pay_run_id`, `company_id`, `attempt`,
  `amount_cents`, `currency`, `republish_seq`. IDs and the amount only: no names or bank details.
  The worker must use the amount from the instruction endpoint; a mismatch is poison. Each stream
  entry has the fields `event_id`, `type` and `envelope` (JSON) (spec R7).
- **DLQ entry** v1: `envelope`, `source_id`, `error_kind`, `error_code`, `error`,
  `delivery_count`, `consumer`, `failed_at`; written with `MAXLEN ~ 10000` (spec C21).
- Examples, validated in CI and read by the contract tests:
  [`contracts/events/examples/`](../contracts/events/examples/).
