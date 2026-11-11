// The scripts/seed.py JSON summary (C33): k6 run -e SEED_JSON=ci-logs/seed.json …
// open() works only in the init context, so every VU reads the same file at start.
const path = __ENV.SEED_JSON;
if (!path) throw new Error("SEED_JSON is not set");
export const seed = JSON.parse(open(path));
if (!seed.runs || !seed.runs.current) throw new Error("seed summary has no runs.current");
