import { execFileSync } from "node:child_process";

import { repoRoot } from "./env";

/**
 * Stop and start one Compose service of the running stack (web LLD §9.2, errors spec). Web →
 * identity is a direct connection (no Toxiproxy), so "identity down" stops the container. The
 * command is the one `scripts/stack.sh` builds for the e2e profiles (core, obs, test).
 */
function compose(...args: string[]): void {
  execFileSync(
    "docker",
    [
      "compose",
      "--env-file",
      ".env",
      "-f",
      "deploy/compose/compose.yaml",
      "-f",
      "deploy/compose/compose.test.yaml",
      "--profile",
      "core",
      "--profile",
      "obs",
      "--profile",
      "test",
      ...args,
    ],
    { cwd: repoRoot(), stdio: ["ignore", "ignore", "inherit"] },
  );
}

export function stopService(service: string): void {
  compose("stop", service);
}

/** Starts the service and waits for its health check (`--wait`). */
export function startService(service: string): void {
  compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", service);
}
