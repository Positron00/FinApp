import type { FakeRedis } from "./fake-redis";
import type { LogCapture } from "./logs";

/** The current test's fake Redis and log capture, set by setup.ts before each test. */
export const t = {} as { redis: FakeRedis; logs: LogCapture };
