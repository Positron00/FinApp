import "server-only";

import { createPrivateKey } from "node:crypto";

import pino from "pino";
import { z } from "zod";

/**
 * Web configuration (web LLD §8). Parsed lazily on first use, so `next build` runs with no
 * secrets in the environment; `instrumentation.ts` calls `loadConfig()` at server start, which
 * logs `config.invalid` (the setting's name only) and exits 2 on a failed check (C29, PAY-605).
 */

const int = (def: number, min = 0) => z.coerce.number().int().min(min).default(def);
const url = (def?: string) => (def === undefined ? z.url() : z.url().default(def));

const LOG_LEVELS = ["trace", "debug", "info", "warn", "error", "fatal"] as const;

const ConfigSchema = z.object({
  ENV: z.enum(["dev", "uat", "prod"]).default("dev"),
  APP_VERSION: z.string().min(1).default("dev"),
  PORT: int(3000, 1),
  PUBLIC_BASE_URL: url("http://127.0.0.1:4100"),
  IDENTITY_BASE_URL: url("http://identity:8000"),
  PAYROLL_BASE_URL: url("http://payroll:8000"),
  COMPANY_ID: z.uuid(),
  // .env stores PEMs with literal "\n" escapes (scripts/dev-env.sh); accept both forms.
  INTERNAL_JWT_PRIVATE_KEY: z
    .string()
    .min(1)
    .transform((v) => v.replace(/\\n/g, "\n")),
  INTERNAL_JWT_KID: z.string().min(1),
  REDIS_HOST: z.string().min(1).default("redis"),
  REDIS_PORT: int(6379, 1),
  REDIS_USERNAME: z.string().min(1).default("web"),
  REDIS_PASSWORD: z.string().min(1),
  REDIS_SOCKET_TIMEOUT_MS: int(500, 1),
  SESSION_SECRET: z.string().min(1),
  SESSION_COOKIE_NAME: z
    .string()
    .regex(/^[A-Za-z0-9_-]+$/)
    .default("sid"),
  SESSION_IDLE_TTL_S: int(43200, 1),
  SESSION_ABS_TTL_S: int(604800, 1),
  SESSION_ROLE_RECHECK_S: int(60),
  SESSION_RECHECK_GRACE_S: int(300),
  SESSION_TOUCH_INTERVAL_S: int(60),
  BFF_RATE_LIMIT_SESSION: int(120, 1),
  BFF_RATE_LIMIT_IP: int(300, 1),
  BFF_TELEMETRY_RATE_LIMIT_IP: int(60, 1),
  BFF_BODY_LIMIT_BYTES: int(65536, 1),
  TRUSTED_PROXY_HOPS: int(0),
  UPSTREAM_READ_TIMEOUT_MS: int(3000, 1),
  UPSTREAM_SLOW_READ_TIMEOUT_MS: int(10000, 1),
  UPSTREAM_GET_RETRIES: int(2),
  UPSTREAM_GET_DEADLINE_MS: int(5000, 1),
  BREAKER_FAILURES: int(5, 1),
  BREAKER_RESET_S: int(30, 1),
  OTEL_EXPORTER_OTLP_ENDPOINT: z.url().optional(),
  // C24: case-insensitive; the docs write it in uppercase.
  LOG_LEVEL: z
    .string()
    .default("INFO")
    .transform((v) => {
      const l = v.toLowerCase();
      return l === "warning" ? "warn" : l === "critical" ? "fatal" : l;
    })
    .pipe(z.enum(LOG_LEVELS)),
});

export type Config = Readonly<z.infer<typeof ConfigSchema>>;
export type ConfigResult = { ok: true; config: Config } | { ok: false; settings: string[] };

/** Startup checks for uat and prod (web LLD §8). Returns the names of the refused settings. */
function startupChecks(c: Config): string[] {
  if (c.ENV === "dev") return [];
  const bad: string[] = [];
  if (/^(dev|test)-/.test(c.INTERNAL_JWT_KID)) bad.push("INTERNAL_JWT_KID");
  if (new URL(c.PUBLIC_BASE_URL).protocol !== "https:") bad.push("PUBLIC_BASE_URL");
  if (c.SESSION_COOKIE_NAME !== "__Host-sid") bad.push("SESSION_COOKIE_NAME");
  if (Buffer.byteLength(c.SESSION_SECRET, "utf8") < 32) bad.push("SESSION_SECRET");
  if (c.TRUSTED_PROXY_HOPS < 1) bad.push("TRUSTED_PROXY_HOPS");
  try {
    const key = createPrivateKey({ key: c.INTERNAL_JWT_PRIVATE_KEY, format: "pem" });
    if (key.asymmetricKeyType !== "ed25519") bad.push("INTERNAL_JWT_PRIVATE_KEY");
  } catch {
    bad.push("INTERNAL_JWT_PRIVATE_KEY");
  }
  return bad;
}

/** Parses and checks an environment. Never throws and never echoes a value. */
export function parseConfig(env: Record<string, string | undefined>): ConfigResult {
  // Empty strings count as unset, as in .env files with `KEY=`.
  const cleaned = Object.fromEntries(
    Object.entries(env).filter(([, v]) => v !== undefined && v !== ""),
  );
  const parsed = ConfigSchema.safeParse(cleaned);
  if (!parsed.success) {
    const settings = [...new Set(parsed.error.issues.map((i) => String(i.path[0] ?? "unknown")))];
    return { ok: false, settings };
  }
  const config = Object.freeze(parsed.data);
  const bad = startupChecks(config);
  return bad.length > 0 ? { ok: false, settings: bad } : { ok: true, config };
}

let cached: Config | undefined;

/** The validated config. Throws (without values) when the environment is invalid. */
export function config(): Config {
  if (cached) return cached;
  const r = parseConfig(process.env);
  if (!r.ok) throw new Error(`config.invalid: ${r.settings.join(", ")}`);
  cached = r.config;
  return cached;
}

/**
 * The startup path (`instrumentation.ts`): on failure logs `config.invalid` once per setting
 * (name only, never the value) and exits with status 2 (C29).
 */
export function loadConfig(
  env: Record<string, string | undefined> = process.env,
  exit: (code: number) => never = (code) => process.exit(code),
  log: pino.Logger = pino({
    base: { service: "web", env: env.ENV ?? "dev", version: env.APP_VERSION ?? "dev" },
    messageKey: "msg",
    timestamp: () => `,"ts":"${new Date().toISOString()}"`,
    formatters: { level: (label) => ({ level: label }) },
  }),
): Config {
  const r = parseConfig(env);
  if (!r.ok) {
    for (const setting of r.settings) {
      log.error({ event: "config.invalid", setting }, "invalid configuration");
    }
    return exit(2);
  }
  cached = r.config;
  return r.config;
}

/** Tests only: forget the cached config so the next `config()` re-reads `process.env`. */
export function resetConfigForTests(): void {
  cached = undefined;
}
