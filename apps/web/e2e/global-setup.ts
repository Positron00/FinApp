import fs from "node:fs";
import path from "node:path";

import { chromium, expect, type FullConfig } from "@playwright/test";

import { APPROVER_EMAIL, authDir, baseUrl, need, readSeed } from "./helpers/env";

/**
 * Global setup (web LLD §9.2; PAY-609 with tests/platform/test_seed.py, C45): validate the seed
 * summary, wait for `/api/readyz`, then sign in once as the owner and as the approver through the
 * login form and store each `storageState`. Any failure stops the run before the first spec.
 */
async function waitReady(url: string, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      if ((await fetch(url)).status === 200) return;
    } catch {
      // not up yet
    }
    if (Date.now() > deadline) throw new Error(`e2e: ${url} not ready after ${timeoutMs} ms`);
    await new Promise((r) => setTimeout(r, 1000));
  }
}

export async function signIn(
  browserBase: string,
  email: string,
  password: string,
  stateFile: string,
): Promise<void> {
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ baseURL: browserBase });
    const page = await context.newPage();
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/dashboard$/, { timeout: 30_000 });
    await context.storageState({ path: stateFile });
  } finally {
    await browser.close();
  }
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  readSeed();
  const base = baseUrl();
  await waitReady(`${base}/api/readyz`);
  const dir = authDir();
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  await signIn(
    base,
    need("SEED_OWNER_EMAIL"),
    need("SEED_OWNER_PASSWORD"),
    path.join(dir, "owner.json"),
  );
  await signIn(
    base,
    APPROVER_EMAIL,
    need("SEED_APPROVER_PASSWORD"),
    path.join(dir, "approver.json"),
  );
}
