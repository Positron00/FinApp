# 0006. Money as integer cents; rounding rules

Status: accepted 2026-11-10
Context: the invariants must hold to the cent. These are: gross = net + tax + deductions; funding = Σ net; and the salaried per-period amounts sum to the annual salary.
Proposal:
- **Storage:** money is `BIGINT` cents (Python `int`; TypeScript `number`, which is safe below 2^53, and amounts are capped at $10M). Rates are integer basis points; hours are integer hundredths. No floats anywhere in money code. A lint check bans `float(` in `domain/`.
- **Salaried gross for period k of n** (n = 26 biweekly, or 12 monthly): `base = annual // n` and `rem = annual % n`, and the period gets `base + (1 if k > n - rem else 0)`. The leftover cents go to the last periods, so Σ over k equals the annual salary exactly.
  - Biweekly periods are indexed from the company's `biweekly_anchor` date. Index = ((start − anchor).days // 14) mod 26 + 1. Every year is treated as 26 periods (27-period years are out of scope).
- **Hourly gross:** `(rate_cents * hours_hundredths + 50) // 100`, rounding half up.
- **Tax:** `(gross * tax_bps + 5000) // 10000`. **Retirement:** `(gross * retirement_bps + 5000) // 10000`. Both round half up.
- **Net:** `gross − tax − retirement`. Validation `tax_bps ≤ 5000` and `retirement_bps ≤ 2000` guarantees net ≥ 0.
- **Totals** are sums of the stored payslip values, never recomputed from rates.
Options considered: Python `Decimal` with banker's rounding (equivalent, but slower and easier to misuse at the JSON boundary), and a money library (an extra dependency for little gain).
Recommendation: the rules above.
Decision: the recommendation above, accepted by the user on 2026-11-10 (plan sign-off).
Consequences: hypothesis property tests over random salaries, rates, periods and hours prove the three invariants.
