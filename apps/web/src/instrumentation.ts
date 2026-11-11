import type { Instrumentation } from "next";

/**
 * Server start (web LLD §7.4). On the Node.js runtime only (the `NEXT_RUNTIME` checks let the
 * bundler drop this code from the edge build), and not during `next build`, which runs with no
 * secrets: validate the config (a failed check logs `config.invalid` and exits 2; C29), then
 * register OTel (lib/otel.ts).
 */
export async function register(): Promise<void> {
  if (
    process.env.NEXT_RUNTIME === "nodejs" &&
    process.env.NEXT_PHASE !== "phase-production-build"
  ) {
    const { startNode } = await import("./lib/otel");
    await startNode();
  }
}

/** Logs `web.server_error` (PAY-602). */
export const onRequestError: Instrumentation.onRequestError = async (err, request, context) => {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { onRequestError: log } = await import("./lib/on-request-error");
    await log(err, request, context);
  }
};
