# Runbook: SigninFailuresSpike

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | identity |
| **Alert** | `sum((increase(auth_signins_total{outcome="failure"}[15m]) and max_over_time(auth_signins_total{outcome="failure"}[1h] offset 15m)) or (auth_signins_total{outcome="failure"} unless max_over_time(auth_signins_total{outcome="failure"}[1h] offset 15m))) > 50` (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-104 (sign in, lockout), PAY-109 (auth audit log), PAY-603; C19 (`outcome` is `success`, `failure` or `rate_limited`) |
| **Dashboard** | Service overview (`service` = identity), `http://127.0.0.1:4101/d/payrun-service-overview` ("Sign-ins") |

## What it means

More than 50 sign-ins failed (wrong email or password, inactive or locked account) in 15 minutes. Requests refused by the login rate limit count as `rate_limited`, not `failure`, so this is attempts that reached the password check. The count is per series: the increase of a series that existed before the window, plus the full value of a new one (a restarted identity whose first metric export already counts failures, which `increase()` reads as 0; C77), so failures split across a restart still add up.

## Impact

Possibly a credential-stuffing or password-guessing attack; possibly real users locked out after a change. Accounts that cross the per-account limit are locked for a while (`auth.lockout`).

## First checks

1. **The shape of it:**

   ```promql
   sum by (outcome) ((increase(auth_signins_total[15m]) and max_over_time(auth_signins_total[1h] offset 15m)) or (auth_signins_total unless max_over_time(auth_signins_total[1h] offset 15m)))
   ```

   Many `failure` and few `success`: likely an attack. A jump in `rate_limited` means the limits are already biting.
2. **Identity logs** (Loki); lines carry hashed identifiers, never emails or passwords:

   ```logql
   {service_name="identity"} | event=~"auth.signin.failed|auth.lockout|auth.signin.rate_limited"
   ```

3. **The auth audit log** (web: Audit → Auth, admin): failed sign-ins and lockouts by account and hashed IP.
4. **BFF side:** is one client sending most of it? `rate_limited_total{scope="ip"}` on the Service overview (`service` = web).

## Likely causes

- Credential stuffing from a few IPs, or spread across many.
- One user or integration retrying with an old password.
- A change that breaks valid sign-ins (password hashing, a migration): then `success` drops to near 0.

## Mitigation

1. For an attack: keep the limits (`AUTH_LOGIN_LIMIT_IP`, `AUTH_LOGIN_LIMIT_ACCOUNT`); block abusive IPs at the edge; tell affected users to reset their passwords if any account shows a success after many failures.
2. For a broken release: roll back.
3. Do not raise the limits during an attack.
4. **Locked-out users:** a user locked out by someone else's failures ends the lock with a password reset (or by accepting an invite); either clears the account's lock, level and failure count. The lock is keyed by email alone, so an attacker can re-lock the account, starting again at level 1 (C74).

## Confirm recovery

- Failures drop below 50 per 15 minutes; the alert has no `for` and resolves at the next evaluation:

  ```promql
  sum((increase(auth_signins_total{outcome="failure"}[15m]) and max_over_time(auth_signins_total{outcome="failure"}[1h] offset 15m)) or (auth_signins_total{outcome="failure"} unless max_over_time(auth_signins_total{outcome="failure"}[1h] offset 15m)))
  ```
