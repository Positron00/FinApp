import { afterEach, beforeEach, inject } from "vitest";

import { applyEnv, TEST_ENV } from "../helpers/env";
import { closeClients } from "./clients";

/**
 * Per-test environment for the real-Redis tests: the dev test env pointed at the container as
 * the `web` ACL user (C12 keys), a fresh config cache, no Redis singleton (the code under test
 * connects for real) and a log capture.
 */
beforeEach(async () => {
  const r = inject("redis");
  applyEnv({
    ...TEST_ENV,
    REDIS_HOST: r.host,
    REDIS_PORT: String(r.port),
    REDIS_USERNAME: "web",
    REDIS_PASSWORD: r.passwords.web,
  });
  const [{ resetConfigForTests }, { setRedisForTests }, { captureLogs }] = await Promise.all([
    import("@/lib/config"),
    import("@/lib/redis"),
    import("../helpers/logs"),
  ]);
  resetConfigForTests();
  setRedisForTests(undefined);
  captureLogs();
});

afterEach(async () => {
  vi.useRealTimers();
  const { setRedisForTests } = await import("@/lib/redis");
  setRedisForTests(undefined);
  await closeClients();
});
