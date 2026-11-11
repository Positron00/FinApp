import type { Instrumentation } from "next";

/** Next's `onRequestError` hook: logs `web.server_error` (route template, digest; PAY-602). */
export const onRequestError: Instrumentation.onRequestError = async (err, _request, context) => {
  const { logger } = await import("./logger");
  const e = err as { digest?: string; stack?: string; message?: string };
  logger().error(
    {
      event: "web.server_error",
      route: context.routePath,
      digest: e.digest ?? null,
      exc: e.stack ?? e.message ?? String(err),
    },
    "server error",
  );
};
