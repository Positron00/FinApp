import { lokiUrl, tempoUrl } from "./env";

/**
 * Tempo and Loki queries for the trace test (testing.md §6; web LLD §9.2; PAY-NFR-09).
 * Tempo: `GET /api/traces/<id>` (OTLP JSON; `batches` in v1, `resourceSpans` otherwise).
 * Loki: `query_range` with `{service_name=~".+"} | trace_id="<id>"` (trace_id is structured
 * metadata on OTLP-ingested logs, so no parser is needed).
 */

interface KeyValue {
  key: string;
  value?: { stringValue?: string };
}
interface ResourceSpans {
  resource?: { attributes?: KeyValue[] };
  scopeSpans?: { spans?: unknown[] }[];
  instrumentationLibrarySpans?: { spans?: unknown[] }[];
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** The `service.name` of every resource that has at least one span in the trace. */
export async function traceServices(traceId: string): Promise<Set<string>> {
  const res = await fetch(`${tempoUrl()}/api/traces/${traceId}`, {
    headers: { Accept: "application/json" },
  });
  if (res.status === 404) return new Set();
  if (!res.ok) throw new Error(`tempo: HTTP ${res.status}`);
  const body = (await res.json()) as {
    batches?: ResourceSpans[];
    resourceSpans?: ResourceSpans[];
    trace?: { resourceSpans?: ResourceSpans[] };
  };
  const batches = body.batches ?? body.resourceSpans ?? body.trace?.resourceSpans ?? [];
  const out = new Set<string>();
  for (const b of batches) {
    const spans = [...(b.scopeSpans ?? []), ...(b.instrumentationLibrarySpans ?? [])].flatMap(
      (s) => s.spans ?? [],
    );
    const name = b.resource?.attributes?.find((a) => a.key === "service.name")?.value?.stringValue;
    if (name && spans.length > 0) out.add(name);
  }
  return out;
}

/** Polls Tempo until spans from every expected service exist, or the deadline passes. */
export async function waitForTraceServices(
  traceId: string,
  expected: readonly string[],
  timeoutMs = 30_000,
): Promise<Set<string>> {
  const deadline = Date.now() + timeoutMs;
  let seen = new Set<string>();
  for (;;) {
    seen = await traceServices(traceId);
    if (expected.every((s) => seen.has(s)) || Date.now() > deadline) return seen;
    await sleep(1000);
  }
}

/** Log line counts per `service_name` for one trace ID over the last `sinceMs`. */
export async function logCountsByService(
  traceId: string,
  sinceMs = 15 * 60_000,
): Promise<Map<string, number>> {
  const end = Date.now();
  const params = new URLSearchParams({
    query: `{service_name=~".+"} | trace_id="${traceId}"`,
    start: String((end - sinceMs) * 1_000_000),
    end: String((end + 60_000) * 1_000_000),
    limit: "5000",
    direction: "forward",
  });
  const res = await fetch(`${lokiUrl()}/loki/api/v1/query_range?${params.toString()}`);
  if (!res.ok) throw new Error(`loki: HTTP ${res.status}`);
  const body = (await res.json()) as {
    data: { result: { stream: Record<string, string>; values: [string, string][] }[] };
  };
  const counts = new Map<string, number>();
  for (const s of body.data.result) {
    const svc = s.stream.service_name ?? "unknown";
    counts.set(svc, (counts.get(svc) ?? 0) + s.values.length);
  }
  return counts;
}

/** Polls Loki until every expected service has at least one line for the trace. */
export async function waitForTraceLogs(
  traceId: string,
  expected: readonly string[],
  timeoutMs = 30_000,
): Promise<Map<string, number>> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const counts = await logCountsByService(traceId);
    if (expected.every((s) => (counts.get(s) ?? 0) > 0) || Date.now() > deadline) return counts;
    await sleep(1000);
  }
}
