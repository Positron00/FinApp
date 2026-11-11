import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

const here = path.dirname(fileURLToPath(import.meta.url));

/**
 * Real-Redis integration tests (`pnpm --filter web int`; web LLD §9.1; PAY-106, PAY-603,
 * PAY-607, C69). `tests/int/redis-container.ts` starts `redis:7.4.11-alpine` with the real
 * `deploy/compose/redis/entrypoint.sh` ACLs through Testcontainers, so Docker is required. The
 * default `test` script excludes `*.int.test.ts` (vitest.config.ts). scripts/ci/web.sh runs this.
 *
 * testcontainers is pinned at 12.0.1, the last release on undici 7: 12.0.2 and later need undici 8,
 * which needs Node >= 22.19 on the 22 line or zstd support (Node 23.8+), while package.json allows
 * any Node >= 22. Revisit when the engines floor rises (CI already uses .nvmrc 24).
 */
export default defineConfig({
  resolve: {
    alias: {
      "@": path.join(here, "src"),
      "server-only": path.join(here, "tests/helpers/empty.ts"),
    },
  },
  test: {
    globals: true,
    environment: "node",
    include: ["tests/**/*.int.test.ts"],
    globalSetup: ["tests/int/redis-container.ts"],
    setupFiles: ["tests/int/setup.ts"],
    restoreMocks: true,
    fileParallelism: false,
    testTimeout: 30_000,
    hookTimeout: 120_000,
  },
});
