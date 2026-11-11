import { randomBytes } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { GenericContainer, type StartedTestContainer, Wait } from "testcontainers";
import type { TestProject } from "vitest/node";

/**
 * Vitest global setup for the real-Redis tests: the Compose image and the real ACL entrypoint
 * (deploy/compose/redis/; platform LLD §2.2; PAY-607). Passwords are generated per run.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const REDIS_DIR = path.resolve(here, "../../../../deploy/compose/redis");
export const REDIS_IMAGE = "redis:7.4.11-alpine"; // the compose.yaml pin

export interface RedisInt {
  host: string;
  port: number;
  passwords: Record<"admin" | "web" | "identity", string>;
}

declare module "vitest" {
  export interface ProvidedContext {
    redis: RedisInt;
  }
}

const secret = () => randomBytes(24).toString("hex");

let container: StartedTestContainer | undefined;

export default async function setup(project: TestProject): Promise<() => Promise<void>> {
  const env: Record<string, string> = {
    REDIS_ADMIN_PASSWORD: secret(),
    REDIS_WEB_PASSWORD: secret(),
    REDIS_IDENTITY_PASSWORD: secret(),
    REDIS_PAYROLL_PASSWORD: secret(),
    REDIS_PAYOUT_WORKER_PASSWORD: secret(),
    REDIS_EXPORTER_PASSWORD: secret(),
  };
  container = await new GenericContainer(REDIS_IMAGE)
    .withEntrypoint(["/bin/sh", "/usr/local/etc/redis/entrypoint.sh"])
    .withCopyFilesToContainer([
      {
        source: path.join(REDIS_DIR, "entrypoint.sh"),
        target: "/usr/local/etc/redis/entrypoint.sh",
      },
      { source: path.join(REDIS_DIR, "redis.conf"), target: "/usr/local/etc/redis/redis.conf" },
    ])
    .withEnvironment(env)
    .withTmpFs({ "/run/redis": "rw,mode=0700,uid=999,gid=1000" })
    .withExposedPorts(6379)
    .withWaitStrategy(Wait.forLogMessage("Ready to accept connections"))
    .start();
  project.provide("redis", {
    host: container.getHost(),
    port: container.getMappedPort(6379),
    passwords: {
      admin: env.REDIS_ADMIN_PASSWORD!,
      web: env.REDIS_WEB_PASSWORD!,
      identity: env.REDIS_IDENTITY_PASSWORD!,
    },
  });
  return async () => {
    await container?.stop();
  };
}
