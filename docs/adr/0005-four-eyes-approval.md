# 0005. Dual approval (four-eyes) as a company setting, default on

Status: accepted 2026-11-10; amended 2026-11-11 (C73)
Context: payroll fraud often starts with one person preparing and releasing payments alone. Many SMB payroll products offer dual control. But very small companies may have one person doing everything.
Options:
1. **Enforced:** the user who last calculated a run can't approve it (403 `approver_must_differ`). Simple and auditable, but a one-person company can't run payroll.
2. **A company setting `require_dual_approval`, default on.** The rule applies when the setting is on, and turning it off is audited. Flexible, but one more setting to build and test.
3. **None:** any approver approves. Simplest, but the audit trail alone records the risk without preventing it.
Recommendation: option 2, defaulting to on. The seed creates a second user (`approver@payrun.example.com`) so the demo and e2e exercise dual control.
Scope of the rule when on:
- The approver must differ from `calculated_by`, **and** from the last editor of bank details (`employees.bank_updated_by`) of any employee in the run since the run was created.
- Retrying a failed payout: the retrier must not be the last bank-detail editor of that employee. This closes the "redirect a failed payout to my own account and retry it alone" path.
- Turning the setting off is audited, and doesn't apply to runs already calculated: the setting is snapshotted onto `pay_runs` at calculate.
- Violations return 403 `approver_must_differ`.

Decision: option 2 (a company setting `require_dual_approval`, default on), chosen by the user on 2026-11-10.
Consequences: `pay_runs.calculated_by` is recorded. e2e signs in as two users. The k6 approve scenario uses the second user.

Amendment 2026-11-11 (C73, H3 security review): the "since the run was created" window let one person prepare and release a payment alone (edit a bank account or add an employee before the run exists, or enter the hours, then approve someone else's calculation). With the setting on, the approver now also differs from the run's creator and from anyone who entered hours on it, and a bank-detail change blocks its editor until a payout for that employee has been released under dual approval by someone else. Retry uses the same window, and the operator who marks an attempt rejected cannot retry that payout. Option 2 itself is unchanged. Refinements from the alignment review (same day): the hours rule means the last editor of any hours row; only employees who get a transfer in the run (net > 0) count, since a no-transfer payout releases and vets nothing; edit and snapshot times come from the database's `clock_timestamp()` read after the employee row lock, never the transaction start, so the row locks order them, and a tie counts as unvetted. On retry, an unvetted bank editor and the operator who marked the attempt rejected are both refused, so the editor should do the `mark_rejected`; if both are refused, a second person re-confirms the bank details and becomes the editor. Not covered in 0.1.0 (accepted risk, C73): changes to an employee's salary or hourly rate.
