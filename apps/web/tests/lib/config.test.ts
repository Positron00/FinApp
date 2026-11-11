import { config, loadConfig, parseConfig, resetConfigForTests } from "@/lib/config";

import { TEST_ENV } from "../helpers/env";

const KEY_LABEL = ["PRIVATE", "KEY"].join(" ");

/** PAY-605, C29, C24, C12: parsing and the uat/prod startup checks. */
const UAT_OK = {
  ...TEST_ENV,
  ENV: "uat",
  PUBLIC_BASE_URL: "https://payrun.example.com",
  SESSION_COOKIE_NAME: "__Host-sid",
  INTERNAL_JWT_KID: "uat-web-2026-11",
  TRUSTED_PROXY_HOPS: "1",
};

function exitSpy() {
  const lines: string[] = [];
  const log = { error: (obj: Record<string, unknown>) => lines.push(JSON.stringify(obj)) };
  const exit = vi.fn((code: number): never => {
    throw new Error(`exit ${code}`);
  });
  return { lines, log: log as unknown as Parameters<typeof loadConfig>[2], exit };
}

describe("config", () => {
  // PAY-605: dev defaults are accepted in dev.
  it("accepts the dev environment with defaults", () => {
    const r = parseConfig(TEST_ENV);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.config).toMatchObject({
      PORT: 3000,
      SESSION_IDLE_TTL_S: 43200,
      SESSION_RECHECK_GRACE_S: 300,
      BFF_BODY_LIMIT_BYTES: 65536,
      REDIS_USERNAME: "web",
    });
    expect(Object.isFrozen(r.config)).toBe(true);
  });

  // PAY-605: uat with proper values passes.
  it("accepts a correct uat environment", () => {
    expect(parseConfig(UAT_OK).ok).toBe(true);
  });

  // PAY-605, C29: each refused uat/prod setting is named (never its value) and exits 2.
  it.each([
    ["INTERNAL_JWT_KID", { INTERNAL_JWT_KID: "dev-web-1" }],
    ["INTERNAL_JWT_KID", { INTERNAL_JWT_KID: "test-web-1" }],
    ["PUBLIC_BASE_URL", { PUBLIC_BASE_URL: "http://payrun.example.com" }],
    ["SESSION_COOKIE_NAME", { SESSION_COOKIE_NAME: "sid" }],
    ["SESSION_SECRET", { SESSION_SECRET: "short-secret-value" }],
    [
      "INTERNAL_JWT_PRIVATE_KEY",
      {
        // Built at runtime so no literal looks like a real key (testing.md §9).
        INTERNAL_JWT_PRIVATE_KEY: `-----BEGIN ${KEY_LABEL}-----\\nnope\\n-----END ${KEY_LABEL}-----`,
      },
    ],
    ["TRUSTED_PROXY_HOPS", { TRUSTED_PROXY_HOPS: "0" }],
  ])("refuses %s in uat", (setting, over) => {
    const env = { ...UAT_OK, ...over };
    const s = exitSpy();
    expect(() => loadConfig(env, s.exit, s.log)).toThrow("exit 2");
    expect(s.exit).toHaveBeenCalledWith(2);
    expect(s.lines.map((l) => JSON.parse(l) as Record<string, string>)).toContainEqual({
      event: "config.invalid",
      setting,
    });
    const value = Object.values(over)[0]!;
    expect(s.lines.join("\n")).not.toContain(value);
  });

  // PAY-605: a missing key exits 2 in every env; C12: the Redis keys are required, REDIS_URL isn't read.
  it.each(["dev", "uat", "prod"])("exits 2 on a missing secret in %s", (env) => {
    const s = exitSpy();
    const rest: Record<string, string | undefined> = {
      ...UAT_OK,
      ENV: env,
      REDIS_URL: "redis://x",
    };
    delete rest.REDIS_PASSWORD;
    expect(() => loadConfig(rest, s.exit, s.log)).toThrow("exit 2");
    expect(s.lines.join()).toContain("REDIS_PASSWORD");
    expect(s.lines.join()).not.toContain("REDIS_URL");
  });

  // C24: LOG_LEVEL is case-insensitive.
  it.each([
    ["INFO", "info"],
    ["info", "info"],
    ["WARNING", "warn"],
    ["DEBUG", "debug"],
  ])("accepts LOG_LEVEL=%s", (v, want) => {
    const r = parseConfig({ ...TEST_ENV, LOG_LEVEL: v });
    expect(r.ok && r.config.LOG_LEVEL).toBe(want);
  });

  // PAY-605: .env PEMs with literal \n escapes are accepted.
  it("unescapes PEM newlines", () => {
    const r = parseConfig({
      ...UAT_OK,
      INTERNAL_JWT_PRIVATE_KEY: TEST_ENV.INTERNAL_JWT_PRIVATE_KEY!.replace(/\n/g, "\\n"),
    });
    expect(r.ok).toBe(true);
  });

  // PAY-605: build safety: config() throws without values rather than exiting.
  it("config() throws a value-free error when invalid", () => {
    delete process.env.SESSION_SECRET;
    resetConfigForTests();
    expect(() => config()).toThrow("config.invalid: SESSION_SECRET");
  });

  // PAY-605: loadConfig returns the config and caches it.
  it("loadConfig returns the parsed config", () => {
    const s = exitSpy();
    expect(loadConfig(TEST_ENV, s.exit, s.log).ENV).toBe("dev");
    expect(config().ENV).toBe("dev");
  });
});
