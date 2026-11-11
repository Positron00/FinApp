# Runbook: EmailOutboxStuck

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | identity (email outbox and relay) |
| **Alert** | `max(identity_email_oldest_pending_age_seconds) > 900 or sum(increase(identity_emails_failed_total[1h])) > 0` for 5 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-101, PAY-105 (invite and reset emails), PAY-110 (email delivery); R25, C20 |
| **Dashboard** | Service overview (`service` = identity), `http://127.0.0.1:4101/d/payrun-service-overview`; Queues and workers ("Identity emails") |

## What it means

Either the oldest pending invite or reset email has waited more than 15 minutes, or at least one email failed permanently in the last hour (it reached `EMAIL_MAX_ATTEMPTS`, 10, and its row became `failed`). Invites still return 201 when SMTP is down: the email waits in `identity.email_outbox` and the relay retries with backoff.

## Impact

Invitees and users who asked for a password reset get no email. They cannot accept the invite or reset their password until it is sent; an admin can resend an invite.

## First checks

1. **Pending and failed:**

   ```promql
   max(identity_email_oldest_pending_age_seconds)
   ```

   ```promql
   sum(increase(identity_emails_failed_total[1h]))
   ```

   ```promql
   max(identity_emails_pending)
   ```

2. **SMTP calls and breaker:**

   ```promql
   sum by (outcome) (rate(external_requests_total{provider="smtp"}[5m]))
   ```

   ```promql
   max(circuit_breaker_state{service_name="identity", target="smtp"})
   ```

3. **Relay logs** (Loki):

   ```logql
   {service_name="identity"} | event=~"email.send_failed|email.failed|email.relay.error"
   ```

   `error` gives the failure kind; `attempts` how far along the row is.
4. **SMTP itself:** in dev and uat, Mailpit (`scripts/stack.sh ps`, UI on `MAILPIT_PORT`); in prod, the provider's status page.

## Likely causes

- SMTP is down or refusing connections (`connect_error`, breaker open).
- Wrong SMTP host, port or credentials after a config change.
- The relay task crashed (`email.relay.error`, or `background.task.crashed` in identity).

## Mitigation

1. Restore SMTP or fix the settings (`SMTP_HOST`, `SMTP_PORT`), then restart identity if the config changed.
2. Pending emails go out on their own once SMTP answers.
3. **Failed** emails are not retried. Tell the admin to resend the invite (web: Users → Resend), or the user to request a new reset link.

## Confirm recovery

- `identity_email_oldest_pending_age_seconds` falls below 900 s and `identity_emails_pending` drains:

  ```promql
  max(identity_emails_pending)
  ```

- The failure half clears one hour after the last permanent failure (`increase(...[1h])` returns to 0); the alert then resolves (R25).
- `email.sent` lines reappear:

  ```logql
  {service_name="identity"} | event="email.sent"
  ```
