// k6 settlement SLO (web LLD §9.3; PAY-NFR-05, PAY-NFR-06). Nightly, after
// `seed.py --employees 500` (500 monthly employees).
//
// `settlement`: shared-iterations, 1 VU, SETTLEMENT_RUNS (default 5) iterations. setup() signs in
// as LOAD_USER_EMAIL (the preparer: payroll_admin only) and approver@payrun.example.com
// (SEED_APPROVER_PASSWORD; R22, C27). Each iteration:
//   1. the next unused monthly period_start after next month (from every page of /api/pay-runs);
//   2. POST /api/pay-runs; every page of its payslips (C8); PUT hours for every hourly payslip in
//      requests of at most 500 entries; POST calculate as the preparer (kind:heavy);
//   3. POST approve as the approver with a fresh Idempotency-Key (kind:heavy);
//   4. poll GET /api/pay-runs/{id} every 2 s until paid or partially_failed (timeout 180 s) and
//      record run_settlement_seconds: from the approve response to the first final status.
// Thresholds: run_settlement_seconds p95 ≤ 60 s (PAY-NFR-06); calculate and approve p95 < 1.5 s
// (PAY-NFR-05); checks > 99%. nightly-load.sh cross-checks payroll_payout_settlement_seconds.
import { check, fail, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

import { allPages } from "./lib/pages.js";
import { get, params, post, put } from "./lib/http.js";
import { login } from "./lib/session.js";

const APPROVER_EMAIL = "approver@payrun.example.com";
const RUNS = Number(__ENV.SETTLEMENT_RUNS || 5);
const settlement = new Trend("run_settlement_seconds");
const runEmployees = new Trend("run_employees");
const settledPaid = new Counter("runs_paid");
const settledPartial = new Counter("runs_partially_failed");

export const options = {
  scenarios: {
    settlement: { executor: "shared-iterations", vus: 1, iterations: RUNS, maxDuration: "40m" },
  },
  thresholds: {
    run_settlement_seconds: ["p(95)<=60"],
    "http_req_duration{name:calculate}": ["p(95)<1500"],
    "http_req_duration{name:approve}": ["p(95)<1500"],
    http_req_failed: ["rate<0.01"],
    checks: ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export function setup() {
  const email = __ENV.LOAD_USER_EMAIL;
  const password = __ENV.LOAD_USER_PASSWORD;
  const approverPassword = __ENV.SEED_APPROVER_PASSWORD;
  if (!email || !password || !approverPassword) {
    fail("LOAD_USER_EMAIL, LOAD_USER_PASSWORD and SEED_APPROVER_PASSWORD must be set");
  }
  return { preparer: login(email, password), approver: login(APPROVER_EMAIL, approverPassword) };
}

function monthStart(y, m) {
  return `${y}-${String(m + 1).padStart(2, "0")}-01`;
}

/** The first monthly period after next month that has no run yet. */
function nextFreeMonth(cookie) {
  const runs = allPages("/api/pay-runs", params(cookie, "/api/pay-runs"));
  const used = new Set(runs.filter((r) => r.schedule === "monthly").map((r) => r.period_start));
  const now = new Date();
  for (let i = 2; i < 2400; i++) {
    const total = now.getUTCFullYear() * 12 + now.getUTCMonth() + i;
    const start = monthStart(Math.floor(total / 12), total % 12);
    if (!used.has(start)) return start;
  }
  fail("no free monthly period");
}

export default function (data) {
  const prep = data.preparer;
  const period = nextFreeMonth(prep);
  const created = post(
    "/api/pay-runs",
    { schedule: "monthly", period_start: period },
    params(prep, "/api/pay-runs"),
  );
  if (!check(created, { "create run 201": (r) => r.status === 201 })) {
    fail(`create run ${period}: HTTP ${created.status}`);
  }
  const run = created.json();
  runEmployees.add(run.employee_count);

  const slips = allPages(
    `/api/pay-runs/${run.id}/payslips`,
    params(prep, "/api/pay-runs/[id]/payslips"),
  );
  const hourly = slips.filter((p) => p.pay_type === "hourly");
  for (let i = 0; i < hourly.length; i += 500) {
    const entries = hourly
      .slice(i, i + 500)
      .map((p) => ({ employee_id: p.employee_id, hours_hundredths: 16000 }));
    const r = put(`/api/pay-runs/${run.id}/hours`, { entries }, params(prep, "hours"));
    check(r, { "hours 200": (x) => x.status === 200 });
  }

  const calc = post(`/api/pay-runs/${run.id}/calculate`, null, params(prep, "calculate", "heavy"));
  if (!check(calc, { "calculate 200": (r) => r.status === 200 })) {
    fail(`calculate: HTTP ${calc.status}`);
  }

  const approve = post(
    `/api/pay-runs/${run.id}/approve`,
    null,
    params(data.approver, "approve", "heavy", {
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
  );
  if (!check(approve, { "approve 200": (r) => r.status === 200 })) {
    fail(`approve: HTTP ${approve.status}`);
  }
  const approvedAt = Date.now();

  let status = approve.json().status;
  while (status !== "paid" && status !== "partially_failed") {
    if (Date.now() - approvedAt > 180_000) break;
    sleep(2);
    const r = get(`/api/pay-runs/${run.id}`, params(prep, "/api/pay-runs/[id]"));
    if (r.status === 200) status = r.json().status;
  }
  const seconds = (Date.now() - approvedAt) / 1000;
  const final = status === "paid" || status === "partially_failed";
  check(status, { "run final within 180 s": () => final });
  if (final) settlement.add(seconds);
  (status === "paid" ? settledPaid : settledPartial).add(final ? 1 : 0);
  console.log(
    `settlement: run ${run.id} (${period}, ${run.employee_count} employees) ${status} after ${seconds.toFixed(1)} s`,
  );
}
