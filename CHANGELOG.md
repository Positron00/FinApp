# Changelog

All notable changes to PayRun are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-11

The first release of PayRun: payroll for small and medium businesses, with journeys J1–J5
end to end. Decisions are recorded in the spec README
(`docs/specs/payrun-0.1/README.md`, R1–R37 and C1–C77) and ADRs 0001–0013.

### Added

- **J1 Access** (identity): invite-only sign-in with roles `admin`, `payroll_admin` and
  `approver`; invites and password resets by email through an outbox relay; invite resend; user
  admin (role changes, deactivation); auth audit log; argon2id passwords; per-account and per-IP
  login limits with lockout; the `bootstrap_owner` CLI for the first owner.
- **J2 Company setup** (payroll): company settings (tax and retirement rates, dual approval,
  biweekly anchor); employees, salaried or hourly, monthly or biweekly, with create, edit,
  deactivate and reactivate; bank details encrypted with AES-256-GCM and key IDs, only the last
  four digits shown; payroll audit log; field-key rotation.
- **J3 Prepare a pay run:** runs per schedule and period with an overlap check, hours entry,
  integer-cent calculation, payslips, run deletion.
- **J4 Approve and pay:** idempotent approval with dual control; payouts through the outbox and
  `stream:payout.requested` to payout-worker, which fetches the instruction from payroll, pays
  through the bank-stub with one idempotency key per attempt, and reports results; re-publish of
  indeterminate attempts; dead-letter stream; `needs_operator` with live run progress in the UI.
- **J5 Recover failed payouts:** retry after a definite rejection (up to 5 attempts) and operator
  resolution of blocked attempts (`redrive`, `mark_rejected`).
- **bank-stub:** a simulated processor with a persistent, idempotent ledger (dev and uat only).
- **Web app:** Next.js 16 pages and BFF with Redis sessions, role re-checks every 60 s, rate
  limits, Origin checks, nonce-based CSP and accessible local UI primitives (ADR 0013).
- **Observability:** OpenTelemetry in every process → Collector → Tempo, Prometheus and Loki →
  Grafana; five dashboards; SLO recording rules; 18 alerts routed through Alertmanager, each with
  a runbook in `docs/runbooks/`; late-settlement detection (`PayoutLateSettlement`).
- **Deployment:** Docker Compose profiles `core`, `obs` and `test`; Kustomize base with dev, uat
  and prod overlays, default-deny NetworkPolicies and External Secrets; versioned migration and
  bootstrap Jobs.
- **Tests and CI:** unit, contract and integration suites with coverage gates (90% pycommon, 85%
  services, 80% web lines); Playwright e2e for J1–J5, trace and accessibility; Schemathesis;
  OpenAPI drift and oasdiff; k6 smoke, full, soak and settlement; 13 fault-injection resilience
  tests; promtool, amtool, kubeconform, Trivy, pip-audit, pnpm audit and gitleaks; a nightly
  workflow (load, resilience, e2e repeat, deep Schemathesis, audits) that files an issue on
  failure; alert drills (`scripts/ci/alert_drills.sh`).
- **Docs:** README, architecture, runbook, testing and API guides, `CLAUDE.md`, and
  `.env.example`.
- Counter alerts (`PayoutDLQ`, `PayoutLateSettlement`, `EmailOutboxStuck`) and their dashboard
  panels also fire on a counter series that first appears above 0, such as a dead letter inside a
  restarted worker's first export interval (C75, found by the live PayoutDLQ drill). All six
  paging alerts pass their drills.

### Security

- Dual approval tightened (C73, amending ADR 0005): with it on, the approver must differ from the
  calculator, the run's creator, anyone who entered hours on the run, and the editor of any bank
  change not yet vetted by a dual-approved payout; retry uses the same window; whoever marks an
  attempt rejected can't retry that payout; redrive of an expired-key attempt is refused; payroll
  audit events are append-only for the runtime role; denied approves and retries are audited.
- Identity hardening (C74): a confirmed password reset or an accepted invite clears the account's
  login lockout, so an attacker can't keep an account (even the only admin) locked out; reset
  requests are padded to a 50 ms floor, so a known and an unknown email take the same time.
- Issuer-bound internal JWT keysets, Redis ACL users per service, per-service DB roles,
  redaction of secrets and personal data in logs and telemetry, non-root images, and 17 accepted
  base-image CVEs recorded with owners and expiry dates in `SECURITY_DEPENDENCIES.md`.

[Unreleased]: https://github.com/Positron00/FinApp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Positron00/FinApp/releases/tag/v0.1.0
