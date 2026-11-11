// k6 smoke through the BFF (web LLD §9.3; PAY-NFR-05): 5 VUs x 30 s as LOAD_USER_EMAIL,
// p95 < 300 ms and < 1% errors. Run with scripts/ci/k6.sh load/smoke.js.
// Per iteration: me, employees, pay runs, the seeded run (seed.runs.current, C33) and its
// payslips, settings.
import http from "k6/http";
import { check, sleep } from "k6";

import { seed } from "./lib/seed.js";
import { TARGET_URL, login } from "./lib/session.js";

export const options = {
  scenarios: { smoke: { executor: "constant-vus", vus: 5, duration: "30s" } },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<300"],
    checks: ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export function setup() {
  const email = __ENV.LOAD_USER_EMAIL;
  const password = __ENV.LOAD_USER_PASSWORD;
  if (!email || !password) throw new Error("LOAD_USER_EMAIL / LOAD_USER_PASSWORD not set");
  return { cookie: login(email, password) };
}

function get(path, name, cookie) {
  const r = http.get(`${TARGET_URL}${path}`, {
    headers: { Cookie: cookie, Accept: "application/json" },
    tags: { name, kind: "simple" },
  });
  check(r, { [`${name} 200`]: (x) => x.status === 200 });
  return r;
}

export default function (data) {
  const run = seed.runs.current;
  get("/api/auth/me", "/api/auth/me", data.cookie);
  get("/api/employees?limit=50", "/api/employees", data.cookie);
  get("/api/pay-runs?limit=20", "/api/pay-runs", data.cookie);
  get(`/api/pay-runs/${run}`, "/api/pay-runs/[id]", data.cookie);
  get(`/api/pay-runs/${run}/payslips?limit=50`, "/api/pay-runs/[id]/payslips", data.cookie);
  get("/api/settings", "/api/settings", data.cookie);
  sleep(1);
}
