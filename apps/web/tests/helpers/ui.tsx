import { focusManager, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";

import { makeQueryClient } from "@/components/providers";

/** Renders with the app's QueryClient defaults (§6.1); the window counts as focused. */
export function renderWithClient(ui: ReactNode) {
  focusManager.setFocused(true);
  const client = makeQueryClient();
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

export interface Call {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

type Reply = { status?: number; body?: unknown; headers?: Record<string, string> } | Error;
type Route = (call: Call) => Reply | undefined;

/** A fetch stub for `/api/*`: `routes` answer by method and path; every call is recorded. */
export function stubApi(routes: Route) {
  const calls: Call[] = [];
  const fetchStub = vi.fn(async (input: string, init: RequestInit = {}) => {
    const call: Call = {
      method: init.method ?? "GET",
      path: input,
      headers: Object.fromEntries(Object.entries((init.headers ?? {}) as Record<string, string>)),
      body: typeof init.body === "string" ? (JSON.parse(init.body) as unknown) : undefined,
    };
    calls.push(call);
    const r = routes(call);
    if (r instanceof Error) throw r;
    if (!r) throw new Error(`unexpected ${call.method} ${call.path}`);
    const status = r.status ?? 200;
    return new Response(status === 204 ? null : JSON.stringify(r.body ?? {}), {
      status,
      headers: { "content-type": "application/json", ...r.headers },
    });
  });
  vi.stubGlobal("fetch", fetchStub);
  return calls;
}

export function apiError(status: number, code: string, message: string, requestId = "req-ui-1") {
  return { status, body: { error: { code, message, request_id: requestId } } };
}
