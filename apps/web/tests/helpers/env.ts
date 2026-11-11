import { generateKeyPairSync } from "node:crypto";

/** A test Ed25519 key pair and a complete dev environment for the web config (web LLD §8). */
const { privateKey, publicKey } = generateKeyPairSync("ed25519");

export const TEST_PRIVATE_PEM = privateKey.export({ type: "pkcs8", format: "pem" }).toString();
export const TEST_PUBLIC_PEM = publicKey.export({ type: "spki", format: "pem" }).toString();

export const IDENTITY = "http://identity.test";
export const PAYROLL = "http://payroll.test";
export const ORIGIN = "http://127.0.0.1:4100";
export const COMPANY_ID = "0193a000-0000-7000-8000-000000000001";
export const SESSION_SECRET = "test-session-secret-0123456789abcdef0123456789";

export const TEST_ENV: Record<string, string> = {
  ENV: "dev",
  APP_VERSION: "test",
  PUBLIC_BASE_URL: ORIGIN,
  IDENTITY_BASE_URL: IDENTITY,
  PAYROLL_BASE_URL: PAYROLL,
  COMPANY_ID,
  INTERNAL_JWT_PRIVATE_KEY: TEST_PRIVATE_PEM,
  INTERNAL_JWT_KID: "test-web-1",
  REDIS_HOST: "127.0.0.1",
  REDIS_PASSWORD: "test-redis-password",
  SESSION_SECRET,
  SESSION_COOKIE_NAME: "sid",
  TRUSTED_PROXY_HOPS: "0",
  LOG_LEVEL: "debug",
  UPSTREAM_GET_DEADLINE_MS: "5000",
};

/** Replaces the web keys of process.env with `env`. */
export function applyEnv(env: Record<string, string | undefined>): void {
  for (const k of Object.keys(TEST_ENV)) delete process.env[k];
  for (const [k, v] of Object.entries(env)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}
