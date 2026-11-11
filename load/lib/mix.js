// The journey mix shared by full.js and soak.js (web LLD §9.3). One iteration is one journey
// operation, picked by weight:
//   reads 70%      one smoke read (me, employees, pay runs, the seeded run, its payslips, settings)
//   employees 15%  POST /api/employees (unique email) then PATCH (deactivate, so the scratch
//                  employees never join a run and later scenarios keep the seeded 500)
//   lifecycle 10%  a scratch biweekly run: create, payslips, hours, calculate (heavy), delete
//   payouts 5%     one page of an approved run's payouts; every 10th walks every page (C8)
import exec from "k6/execution";
import { check, fail, sleep } from "k6";

import { allPages } from "./pages.js";
import { seed } from "./seed.js";
import { login } from "./session.js";
import { del, get, params, patch, post, put, scratchBiweeklyStart } from "./http.js";

const APPROVER_EMAIL = "approver@payrun.example.com";

function ok(r, what, status = 200) {
  return check(r, { [`${what} ${status}`]: (x) => x.status === status });
}

/** A run with payouts for the payouts reads: the biggest paid run, else a fresh scratch one. */
function payoutsRun(cookie, anchor) {
  for (const status of ["paid", "partially_failed"]) {
    const r = get(`/api/pay-runs?status=${status}&limit=100`, params(cookie, "/api/pay-runs"));
    if (r.status !== 200) fail(`list ${status} runs: HTTP ${r.status}`);
    const runs = r.json().items.sort((a, b) => b.employee_count - a.employee_count);
    if (runs.length > 0) return runs[0].id;
  }
  const password = __ENV.SEED_APPROVER_PASSWORD;
  if (!password) fail("SEED_APPROVER_PASSWORD is not set (scripts/ci/k6.sh passes it)");
  const approver = login(APPROVER_EMAIL, password);
  const run = createScratchRun(cookie, anchor, {});
  if (!run) fail("could not create a scratch run for the payouts reads");
  enterHours(cookie, run.id, {});
  if (
    post(`/api/pay-runs/${run.id}/calculate`, null, params(cookie, "calculate", "heavy")).status !==
    200
  ) {
    fail("calculate the payouts run");
  }
  const a = post(
    `/api/pay-runs/${run.id}/approve`,
    null,
    params(approver, "approve", "heavy", { headers: { "Idempotency-Key": crypto.randomUUID() } }),
  );
  if (a.status !== 200) fail(`approve the payouts run: HTTP ${a.status}`);
  for (let i = 0; i < 90; i++) {
    const s = get(`/api/pay-runs/${run.id}`, params(cookie, "/api/pay-runs/[id]")).json().status;
    if (s === "paid" || s === "partially_failed") return run.id;
    sleep(2);
  }
  fail("the payouts run did not settle in 180 s");
}

export function setupMix() {
  const email = __ENV.LOAD_USER_EMAIL;
  const password = __ENV.LOAD_USER_PASSWORD;
  if (!email || !password) fail("LOAD_USER_EMAIL / LOAD_USER_PASSWORD not set");
  const cookie = login(email, password);
  const settings = get("/api/settings", params(cookie, "/api/settings"));
  if (settings.status !== 200) fail(`settings: HTTP ${settings.status}`);
  const anchor = settings.json().biweekly_anchor;
  return { cookie, anchor, payoutsRun: payoutsRun(cookie, anchor) };
}

const READS = [
  ["/api/auth/me", "/api/auth/me"],
  ["/api/employees?limit=50", "/api/employees"],
  ["/api/pay-runs?limit=20", "/api/pay-runs"],
  [`/api/pay-runs/${seed.runs.current}`, "/api/pay-runs/[id]"],
  [`/api/pay-runs/${seed.runs.current}/payslips?limit=50`, "/api/pay-runs/[id]/payslips"],
  ["/api/settings", "/api/settings"],
];

export function read(data, tags = {}) {
  const [path, name] = READS[Math.floor(Math.random() * READS.length)];
  ok(get(path, params(data.cookie, name, "simple", { tags })), name);
}

export function employeeWrite(data, tags = {}) {
  const id = `${exec.vu.idInTest}-${exec.vu.iterationInScenario}-${Math.floor(Math.random() * 1e9)}`;
  const account = `${Math.floor(1e8 + Math.random() * 8e8)}7`;
  const r = post(
    "/api/employees",
    {
      name: `Load ${id}`,
      email: `load-emp-${id}@payrun.example.com`,
      pay_type: "salaried",
      schedule: "biweekly",
      annual_salary_cents: 5_000_000,
      bank_routing: "021000021",
      bank_account: account,
    },
    params(data.cookie, "/api/employees", "simple", { tags }),
  );
  if (!ok(r, "POST /api/employees", 201)) return;
  ok(
    patch(
      `/api/employees/${r.json().id}`,
      { status: "inactive" },
      params(data.cookie, "/api/employees/[id]", "simple", { tags }),
    ),
    "PATCH /api/employees/[id]",
  );
}

function createScratchRun(cookie, anchor, tags) {
  for (let i = 0; i < 5; i++) {
    const r = post(
      "/api/pay-runs",
      { schedule: "biweekly", period_start: scratchBiweeklyStart(anchor) },
      params(cookie, "/api/pay-runs", "simple", { tags }),
    );
    if (r.status === 201) return r.json();
    if (r.status !== 409) {
      ok(r, "POST /api/pay-runs", 201);
      return null;
    }
  }
  return null;
}

function enterHours(cookie, runId, tags) {
  const slips = allPages(
    `/api/pay-runs/${runId}/payslips`,
    params(cookie, "/api/pay-runs/[id]/payslips", "simple", { tags }),
  );
  const hourly = slips.filter((p) => p.pay_type === "hourly");
  for (let i = 0; i < hourly.length; i += 500) {
    const entries = hourly
      .slice(i, i + 500)
      .map((p) => ({ employee_id: p.employee_id, hours_hundredths: 8000 }));
    ok(
      put(`/api/pay-runs/${runId}/hours`, { entries }, params(cookie, "hours", "simple", { tags })),
      "PUT hours",
    );
  }
}

export function runLifecycle(data, tags = {}) {
  const run = createScratchRun(data.cookie, data.anchor, tags);
  if (!run) return;
  enterHours(data.cookie, run.id, tags);
  ok(
    post(
      `/api/pay-runs/${run.id}/calculate`,
      null,
      params(data.cookie, "calculate", "heavy", { tags }),
    ),
    "calculate",
  );
  ok(
    del(`/api/pay-runs/${run.id}`, params(data.cookie, "/api/pay-runs/[id]", "simple", { tags })),
    "DELETE run",
    204,
  );
}

export function payoutsRead(data, tags = {}) {
  const p = params(data.cookie, "/api/pay-runs/[id]/payouts", "simple", { tags });
  if (Math.random() < 0.1) {
    allPages(`/api/pay-runs/${data.payoutsRun}/payouts`, p);
    return;
  }
  ok(get(`/api/pay-runs/${data.payoutsRun}/payouts?limit=50`, p), "payouts page");
}

/** One journey operation by weight; `writeShare` scales the non-read share (soak: 10%). */
export function journey(data, tags = {}, writeShare = 0.3) {
  const x = Math.random();
  const reads = 1 - writeShare;
  if (x < reads) return read(data, tags);
  const w = (x - reads) / writeShare; // 0..1 across the writes, in the 15:10:5 ratio
  if (w < 0.5) return employeeWrite(data, tags);
  if (w < 5 / 6) return runLifecycle(data, tags);
  return payoutsRead(data, tags);
}
