import { afterEach, beforeEach } from "vitest";

import { applyEnv, TEST_ENV } from "./env";

/**
 * Per-test reset for server modules: the dev test environment, a fresh fake Redis, fresh
 * breakers, a fresh config cache and a log capture. Component (jsdom) tests import none of the
 * server modules, so this file only touches them through dynamic imports.
 */
applyEnv(TEST_ENV);

beforeEach(async () => {
  applyEnv(TEST_ENV);
  if (typeof window !== "undefined") return; // jsdom component tests
  const [{ resetConfigForTests }, { setRedisForTests }, { FakeRedis }, { resetBreakersForTests }] =
    await Promise.all([
      import("@/lib/config"),
      import("@/lib/redis"),
      import("./fake-redis"),
      import("@/lib/api/breaker"),
    ]);
  const { resetRecheckForTests } = await import("@/lib/require-session");
  const { resetSigningKeyForTests } = await import("@/lib/internal-jwt");
  const { resetClientIpLogForTests } = await import("@/lib/client-ip");
  const { captureLogs } = await import("./logs");
  const { t } = await import("./state");
  resetConfigForTests();
  t.redis = new FakeRedis();
  setRedisForTests(t.redis);
  resetBreakersForTests();
  resetRecheckForTests();
  resetSigningKeyForTests();
  resetClientIpLogForTests();
  t.logs = captureLogs();
});

afterEach(() => {
  vi.useRealTimers();
});
