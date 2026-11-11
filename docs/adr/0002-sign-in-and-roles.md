# 0002. Sign-in pattern A, invite-only, three roles

Status: accepted 2026-11-10
Context:
- Payroll holds salaries and bank details, and approvals must be attributable to a person. The user chose built-in accounts (pattern A) at intake on 2026-11-10.
- Pattern A's default includes self sign-up with email verification. But staff accounts at a payroll provider are granted by the company, not self-created.
- 0.1.0 has one seeded company.
Options:
1. **Invite-only.** An admin invites by email; the invitee sets a password through a one-time link (hashed token, 72 h). This reuses the verification-token machinery, and the first admin (owner) is seeded. There's no public sign-up endpoint.
2. **Self sign-up plus email verification** (pattern A default). A signed-up user would have no company or roles until someone assigns them, so it adds a pending-membership flow for no 0.1.0 value, plus a public abuse surface.
Roles: `admin` (users, company settings, audit), `payroll_admin` (employees, preparing runs), `approver` (approve runs, retry failed payouts, and resolve blocked `needs_operator` payouts with `redrive` or `mark_rejected`; PAY-502, spec R8). Roles are additive, and the seeded owner holds all three.
Email change: deferred to after 0.1 (out of scope).
Recommendation: option 1, with the three roles above.
Decision: the recommendation above, accepted by the user on 2026-11-10 (plan sign-off).
Consequences:
- identity has no public sign-up route, so the abuse surface is limited to login, reset and invite-accept, all rate-limited.
- **The first owner in every environment:** `python -m identity.bootstrap_owner --email … --company-id …`, a one-shot CLI run as a Compose service or Kubernetes Job with the identity migrator credentials. It works only while `users` is empty, and emails an invite link. There's no bootstrap HTTP endpoint.
- `scripts/seed.py` (dev and CI only) creates the second approver and the load-test user through the admin invite API, accepting each invite with the token read from Mailpit's API. No back door exists.
- **Salary visibility:** all three roles can read salaries and payslips; a 0.1.0 company is small and every role works on payroll. A narrower `admin` (users and settings only) is a later option.
- **Role changes take effect within `SESSION_ROLE_RECHECK_S` (60 s)** while identity is reachable: the BFF re-checks `roles_version` and status. If identity is unreachable, cached roles are kept for up to `SESSION_RECHECK_GRACE_S` (300 s), then the BFF returns 503, so a demotion can't be bypassed for longer than that window (spec C37).
- **Bootstrap flags (spec R16):** `--if-empty` makes re-runs a no-op (exit 0). `--password-env VAR` (dev only) creates an active owner with no invite email, for the Compose stack.
- Revisit for multi-company or SSO (pattern C).
