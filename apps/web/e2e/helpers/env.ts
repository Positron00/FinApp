import fs from "node:fs";

import { z } from "zod";

/** Settings the e2e suite reads (filled from `.env` by playwright.config.ts). Never logged. */
export function need(key: string): string {
  const v = process.env[key];
  if (!v) throw new Error(`e2e: ${key} is not set (run scripts/dev-env.sh)`);
  return v;
}

export const baseUrl = (): string => need("BASE_URL");
export const tempoUrl = (): string => `http://127.0.0.1:${need("TEMPO_PORT")}`;
export const lokiUrl = (): string => `http://127.0.0.1:${need("LOKI_PORT")}`;
export const authDir = (): string => need("E2E_AUTH_DIR");

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
