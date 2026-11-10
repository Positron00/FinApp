# 0001. Service map: identity, payroll, payout-worker, bank-stub

Status: accepted 2026-11-10
Context: PayRun has five journeys (plan §3). The skill splits services only where a boundary pays for itself: different data ownership, a different scaling or failure profile, a slow external dependency to isolate, or background work. Data is sensitive (credentials, salaries, bank details). The scale is small (≤ 20 req/s, ≤ 500 employees per run).
Options:
1. **identity + payroll + payout-worker + bank-stub.**
   - identity isolates credentials from money data.
   - payroll owns every money rule and the run state machine.
   - payout-worker isolates the slow and unreliable bank dependency, and makes crash-mid-payout testable.
   - bank-stub gives the resilient client, Toxiproxy faults and the double-payment checks a real HTTP target.
   - Cost: 4 Python deployables plus web.
2. **payroll (with auth tables) + payout-worker.** Fewer moving parts, but the payroll DB role could read password hashes, and payroll changes would risk auth. Pattern A expects an identity service.
3. **Split payroll further** (employees service, pay-run service). Every calculation and approval would need data from both, which adds synchronous calls and distributed consistency for no benefit at this scale.
4. **Payouts as an in-process background task in payroll.** Bank latency and retries would share the API's event loop and failure domain, and there'd be no clean worker crash test.
Recommendation: option 1. bank-stub is test infrastructure that runs in the Compose `core` profile and in the dev and uat Kubernetes overlays, never in prod. It persists its ledger in its own `bank_stub` schema (`UNIQUE(idempotency_key)`), so idempotency survives restarts and replicas. In prod it's replaced by a provider adapter behind the same client interface (POST with an idempotency key, GET by key).
Decision: the recommendation above, accepted by the user on 2026-11-10 (plan sign-off).
Consequences:
- Five images to build and scan.
- The call graph is depth 2 with no synchronous cycles.
- payout-worker needs its own schema (`payouts`) for idempotency and disbursement records, and its own JWT signing key for calls to payroll.
- Revisit if payroll grows beyond about 15 endpoints per domain, or if a second payment rail appears.
