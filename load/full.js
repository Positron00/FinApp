// k6 full load through the BFF (web LLD §9.3; plan §10; PAY-NFR-04, PAY-NFR-05).
// Nightly: scripts/ci/nightly-load.sh, after `seed.py --employees 500`.
//
// `peak`: ramping-arrival-rate 0 → 30 journey operations/s over 1 min, held for 10 min (peak 20 ×
// 1.5), mirroring the journey mix in lib/mix.js (70% reads, 15% employee writes, 10% scratch run
// lifecycle, 5% payouts reads). FULL_HOLD (default 10m) shortens the hold for a local check.
// Thresholds (PAY-NFR-05): errors < 1%; p95 < 300 ms for reads and simple writes; p95 < 1500 ms
// for calculate (kind:heavy).
import { journey, setupMix } from "./lib/mix.js";

export const options = {
  scenarios: {
    peak: {
      executor: "ramping-arrival-rate",
      startRate: 0,
      timeUnit: "1s",
      preAllocatedVUs: 30,
      maxVUs: 150,
      stages: [
        { target: 30, duration: "1m" },
        { target: 30, duration: __ENV.FULL_HOLD || "10m" },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{kind:simple}": ["p(95)<300"],
    "http_req_duration{kind:heavy}": ["p(95)<1500"],
    checks: ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
  setupTimeout: "240s",
};

export function setup() {
  return setupMix();
}

export default function (data) {
  journey(data);
}
