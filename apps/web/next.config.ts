import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

// The repo root: standalone output traces workspace packages from here (deployment.md).
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

// Static security headers for every path (web LLD §5.6). HSTS depends on the runtime ENV,
// which isn't known at build time, so proxy.ts adds it in prod.
const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: repoRoot,
  poweredByHeader: false,
  reactStrictMode: true,
  serverExternalPackages: ["pino", "ioredis"],
  transpilePackages: ["@payrun/api-client"],
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
