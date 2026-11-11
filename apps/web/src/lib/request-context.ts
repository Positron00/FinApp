import "server-only";

import { AsyncLocalStorage } from "node:async_hooks";

import { processSingleton } from "./process-singleton";

/** Per-request context for the logger mixin and the service clients (web LLD §1.3). */
export interface RequestContext {
  requestId: string;
}

export const requestContext = processSingleton(
  "request-context",
  () => new AsyncLocalStorage<RequestContext>(),
);

export function currentRequestId(): string | undefined {
  return requestContext.getStore()?.requestId;
}
