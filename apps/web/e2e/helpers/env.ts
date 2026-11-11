import fs from "node:fs";

import { z } from "zod";

/** Settings the e2e suite reads (filled from `.env` by playwright.config.ts). Never logged. */
export function need(key: string): string {
  const v = process.env[key];
  if (!v) throw new Error(`e2e: ${key} is not set (run scripts/dev-env.sh)`);
  return v;
}

export const baseUrl = (): string => need("BASE_URL");
export const repoRoot = (): string => need("E2E_REPO_ROOT");
export const tempoUrl = (): string => `http://127.0.0.1:${need("TEMPO_PORT")}`;
export const lokiUrl = (): string => `http://127.0.0.1:${need("LOKI_PORT")}`;
export const authDir = (): string => need("E2E_AUTH_DIR");
export const mailpitUrl = (): string => `http://127.0.0.1:${need("MAILPIT_PORT")}`;
export const toxiproxyUrl = (): string => `http://127.0.0.1:${need("TOXIPROXY_PORT")}`;
/** identity's test-profile port: a direct call that bypasses the BFF (role-change spec, C45). */
export const identityDirectUrl = (): string => `http://127.0.0.1:${need("IDENTITY_PORT")}`;
/** payroll's test-profile port: the worker's internal result endpoint (blocked case, C44). */
export const payrollDirectUrl = (): string => `http://127.0.0.1:${need("PAYROLL_PORT")}`;

export const APPROVER_EMAIL = "approver@payrun.example.com";

/** The `scripts/seed.py` JSON summary (platform LLD §3.15; PAY-609, C33). */
export const SeedSummary = z.object({
  mode: z.enum(["skeleton", "full"]),
  company_id: z.uuid(),
  users: z.object({ owner: z.uuid(), approver: z.uuid(), load: z.uuid() }),
  employees: z.number().int().min(5),
  runs: z.object({ current: z.uuid(), draft: z.uuid() }),
});
export type SeedSummary = z.infer<typeof SeedSummary>;

export function readSeed(): SeedSummary {
  const file = need("SEED_JSON");
  const parsed = SeedSummary.parse(JSON.parse(fs.readFileSync(file, "utf8")));
  if (parsed.company_id !== need("COMPANY_ID")) {
    throw new Error("e2e: seed company_id differs from COMPANY_ID (C28)");
  }
  return parsed;
}
