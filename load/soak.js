// k6 soak through the BFF (web LLD §9.3; PAY-NFR-04, PAY-NFR-05). Nightly.
//
// `soak`: constant-arrival-rate 5 journey operations/s for 30 min (SOAK_DURATION overrides),
// reads plus 10% writes (lib/mix.js). Same thresholds as full.js, plus no upward drift: the p95
// of the last 5 minutes must be within 20% of the first 5 minutes'. Every request is tagged
// `window:first|middle|last`; the two sub-metrics land in the summary export, and
// scripts/ci/nightly-load.sh compares them (a k6 handleSummary can't fail the run).
import exec from "k6/execution";

import { journey, setupMix } from "./lib/mix.js";

const DURATION = __ENV.SOAK_DURATION || "30m";
const WINDOW_MS = Number(__ENV.SOAK_WINDOW_S || 300) * 1000;

function durationMs(d) {
  const m = /^(\d+)(s|m|h)$/.exec(d);
  if (!m) throw new Error(`SOAK_DURATION must look like 30m: ${d}`);
  return Number(m[1]) * { s: 1000, m: 60000, h: 3600000 }[m[2]];
}
const TOTAL_MS = durationMs(DURATION);

export const options = {
  scenarios: {
    soak: {
      executor: "constant-arrival-rate",
      rate: 5,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{kind:simple}": ["p(95)<300"],
    "http_req_duration{kind:heavy}": ["p(95)<1500"],
    checks: ["rate>0.99"],
    // Declared so the summary has these sub-metrics; the drift check is in handleSummary.
    "http_req_duration{window:first,kind:simple}": ["p(95)>=0"],
    "http_req_duration{window:last,kind:simple}": ["p(95)>=0"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
  setupTimeout: "240s",
};

export function setup() {
  return setupMix();
}

export default function (data) {
  const elapsed = Date.now() - exec.scenario.startTime;
  const window =
    elapsed < WINDOW_MS ? "first" : elapsed >= TOTAL_MS - WINDOW_MS ? "last" : "middle";
  journey(data, { window }, 0.1);
}
