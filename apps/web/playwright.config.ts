import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright against the running Compose stack (web LLD §9.2; C12, C51).
 * Start it first: `scripts/stack.sh up` then `scripts/seed.py --skeleton --out ci-logs/seed.json`.
 * Run: `pnpm --filter web exec playwright test --grep @skeleton`.
 *
 * Settings come from the environment, else from the repo's `.env` (written by scripts/dev-env.sh);
 * no value is ever printed. The browser always uses 127.0.0.1, never localhost (C12).
 */
const here = path.dirname(fileURLToPath(import.meta.url));
export const repoRoot = path.resolve(here, "../..");

function loadDotEnv(file: string): void {
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, "utf8").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const i = line.indexOf("=");
    const key = line.slice(0, i).trim();
    let value = line.slice(i + 1);
    if (value.length >= 2 && value[0] === value.at(-1) && (value[0] === '"' || value[0] === "'")) {
      value = value.slice(1, -1);
    }
    if (process.env[key] === undefined) process.env[key] = value;
  }
}
loadDotEnv(path.join(repoRoot, ".env"));

process.env.SEED_JSON ??= path.join(repoRoot, "ci-logs/seed.json");
process.env.E2E_AUTH_DIR ??= path.join(repoRoot, "ci-logs/e2e-auth");
const baseURL = process.env.BASE_URL ?? `http://127.0.0.1:${process.env.WEB_PORT ?? "4100"}`;
process.env.BASE_URL = baseURL;

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts$/,
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  outputDir: path.join(repoRoot, "ci-logs/playwright/test-results"),
  reporter: [
    ["list"],
    ["html", { outputFolder: path.join(repoRoot, "ci-logs/playwright/report"), open: "never" }],
  ],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], baseURL } }],
});
