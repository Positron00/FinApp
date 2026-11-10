# 0005. Dual approval (four-eyes) as a company setting, default on

Status: accepted 2026-11-10
Context: payroll fraud often starts with one person preparing and releasing payments alone. Many SMB payroll products offer dual control. But very small companies may have one person doing everything.
Options:
1. **Enforced:** the user who last calculated a run can't approve it (403 `approver_must_differ`). Simple and auditable, but a one-person company can't run payroll.
2. **A company setting `require_dual_approval`, default on.** The rule applies when the setting is on, and turning it off is audited. Flexible, but one more setting to build and test.
3. **None:** any approver approves. Simplest, but the audit trail alone records the risk without preventing it.
Recommendation: option 2, defaulting to on. The seed creates a second user (`approver@payrun.local`) so the demo and e2e exercise dual control.
Scope of the rule when on:
- The approver must differ from `calculated_by`, **and** from the last editor of bank details (`employees.bank_updated_by`) of any employee in the run since the run was created.
- Retrying a failed payout: the retrier must not be the last bank-detail editor of that employee. This closes the "redirect a failed payout to my own account and retry it alone" path.
- Turning the setting off is audited, and doesn't apply to runs already calculated: the setting is snapshotted onto `pay_runs` at calculate.
- Violations return 403 `approver_must_differ`.

Decision: option 2 (a company setting `require_dual_approval`, default on), chosen by the user on 2026-11-10.
Consequences: `pay_runs.calculated_by` is recorded. e2e signs in as two users. The k6 approve scenario uses the second user.
