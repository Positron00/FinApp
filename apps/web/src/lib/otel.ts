import "server-only";

import { randomUUID } from "node:crypto";

import { IORedisInstrumentation } from "@opentelemetry/instrumentation-ioredis";
import { OTLPLogExporter } from "@opentelemetry/exporter-logs-otlp-http";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-http";
import { BatchLogRecordProcessor } from "@opentelemetry/sdk-logs";
import { PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import { registerOTel } from "@vercel/otel";

import { loadConfig } from "./config";
import { metricsApi } from "./metrics";
import { originRegex } from "./otel-util";
import { redis } from "./redis";

/**
 * `service.instance.id`: one random UUID per process, as the Python SDK's default resource gives
 * every pycommon service. Without it two web replicas (the uat and prod overlays run 2) export
 * identical series, which collide in the Collector's Prometheus exporter. `HOSTNAME` is not used:
 * the image sets `HOSTNAME=0.0.0.0` (the Next.js standalone server's bind address), so it would be
 * the same in every replica.
 */
export const SERVICE_INSTANCE_ID = randomUUID();

/**
 * OTel for the Node.js server (web LLD §7.4): traces propagate to identity and payroll
 * (`propagateContextUrls` from their base URLs, PAY-NFR-09); metrics through an OTLP reader
 * every 15 s; pino lines through an OTLP log processor. Exporters read
 * OTEL_EXPORTER_OTLP_ENDPOINT; the sampler is OTEL_TRACES_SAMPLER (C23). Each process has its own
 * `service.instance.id` (SERVICE_INSTANCE_ID).
 */
export async function startNode(): Promise<void> {
  const cfg = loadConfig();
  const otlp = cfg.OTEL_EXPORTER_OTLP_ENDPOINT !== undefined;
  registerOTel({
    serviceName: "web",
    attributes: {
      "service.version": cfg.APP_VERSION,
      "deployment.environment.name": cfg.ENV,
      "service.instance.id": SERVICE_INSTANCE_ID,
    },
    instrumentations: [
      "fetch",
      // The command name only, never its arguments (no session key or limiter subject in spans).
      new IORedisInstrumentation({ dbStatementSerializer: (cmd) => cmd }),
    ],
    instrumentationConfig: {
      fetch: {
        propagateContextUrls: [
          originRegex(cfg.IDENTITY_BASE_URL),
          originRegex(cfg.PAYROLL_BASE_URL),
        ],
      },
    },
    metricReaders: otlp
      ? [
          new PeriodicExportingMetricReader({
            exporter: new OTLPMetricExporter(),
            exportIntervalMillis: 15_000,
          }),
        ]
      : [],
    logRecordProcessors: otlp
      ? [new BatchLogRecordProcessor({ exporter: new OTLPLogExporter() })]
      : [],
  });
  // Pre-initialised counters (C72) export from the first interval, not from the first request.
  metricsApi.init();
  // Connect to Redis at start, so the first request doesn't race the connection.
  redis();
}
