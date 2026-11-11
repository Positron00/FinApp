import "server-only";

import { trace } from "@opentelemetry/api";
import { logs, SeverityNumber } from "@opentelemetry/api-logs";
import pino from "pino";

import { config } from "./config";
import { redact } from "./redact";
import { currentRequestId } from "./request-context";

/**
 * The pino root logger (web LLD §7.1): JSON lines with `ts`, `level`, `event`, `msg`,
 * `trace_id`, `span_id` and `request_id`, deep key redaction, and base fields `service`, `env`
 * and `version`. When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, each line is also emitted as an
 * OTel log record (exported by the logger provider that instrumentation.ts registers).
 */

const SEVERITY: Record<string, SeverityNumber> = {
  trace: SeverityNumber.TRACE,
  debug: SeverityNumber.DEBUG,
  info: SeverityNumber.INFO,
  warn: SeverityNumber.WARN,
  error: SeverityNumber.ERROR,
  fatal: SeverityNumber.FATAL,
};

type Sink = { write(line: string): void };

/** A pino stream that forwards each JSON line to the OTel logs API, inside the active context. */
export const otelSink: Sink = {
  write(line: string) {
    let rec: Record<string, unknown>;
    try {
      rec = JSON.parse(line) as Record<string, unknown>;
    } catch {
      return;
    }
    const { level, msg, ts, ...attributes } = rec;
    const label = typeof level === "string" ? level : "info";
    logs.getLogger("web").emit({
      severityNumber: SEVERITY[label] ?? SeverityNumber.INFO,
      severityText: label.toUpperCase(),
      body: typeof msg === "string" ? msg : "",
      timestamp: typeof ts === "string" ? new Date(ts) : new Date(),
      attributes: Object.fromEntries(
        Object.entries(attributes).map(([k, v]) => [
          k,
          typeof v === "object" && v !== null
            ? JSON.stringify(v)
            : (v as string | number | boolean),
        ]),
      ),
    });
  },
};

function build(dest?: Sink): pino.Logger {
  let env = "dev";
  let version = "dev";
  let level = "info";
  let otlp = false;
  try {
    const c = config();
    env = c.ENV;
    version = c.APP_VERSION;
    level = c.LOG_LEVEL;
    otlp = c.OTEL_EXPORTER_OTLP_ENDPOINT !== undefined;
  } catch {
    // No valid config yet (build, or a failed startup): log with the defaults.
  }
  const options: pino.LoggerOptions = {
    level,
    base: { service: "web", env, version },
    messageKey: "msg",
    timestamp: () => `,"ts":"${new Date().toISOString()}"`,
    formatters: {
      level: (label) => ({ level: label }),
      log: (obj) => redact(obj) as Record<string, unknown>,
    },
    mixin() {
      const out: Record<string, string> = {};
      const span = trace.getActiveSpan();
      if (span) {
        const sc = span.spanContext();
        out.trace_id = sc.traceId;
        out.span_id = sc.spanId;
      }
      const requestId = currentRequestId();
      if (requestId) out.request_id = requestId;
      return out;
    },
  };
  if (dest) return pino(options, dest);
  const stdout = pino.destination({ dest: 1, sync: true });
  if (!otlp) return pino(options, stdout);
  return pino(options, pino.multistream([{ stream: stdout }, { stream: otelSink }]));
}

let root: pino.Logger | undefined;

/** The root logger, built on first use (after config is loaded). */
export function logger(): pino.Logger {
  root ??= build();
  return root;
}

/** Tests only: capture log lines in `sink` (or rebuild the default logger with `undefined`). */
export function setLogSinkForTests(sink: Sink | undefined): void {
  root = sink ? build(sink) : undefined;
}

/** A user ID for logs: the first 16 hex chars of HMAC(SESSION_SECRET, user_id) (§7.1). */
export { userIdHash } from "./hmac";
