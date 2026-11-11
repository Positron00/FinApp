import "server-only";

import {
  createIdentityClient,
  createPayrollClient,
  type IdentityClient,
  type PayrollClient,
} from "@payrun/api-client";
import type { Middleware } from "openapi-fetch";

import { config } from "../config";
import { internalToken, type Aud, type TokenClaims } from "../internal-jwt";
import { resilientFetch, type ResilientOptions } from "./resilient-fetch";

/**
 * Per-request service clients (web LLD §1.3, §3.4.2): openapi-fetch over the generated paths,
 * with a middleware that adds `Authorization: Bearer <fresh JWT>`, `X-Request-ID`, and (identity
 * only) `X-Client-IP` when the client IP is known. It's omitted when unknown (C16).
 */
export interface ClientContext {
  requestId: string;
  ip: string | null;
  claims: () => TokenClaims;
  fetchOptions?: ResilientOptions;
}

function authMiddleware(aud: Aud, ctx: ClientContext, sendClientIp: boolean): Middleware {
  return {
    async onRequest({ request }) {
      request.headers.set("Authorization", `Bearer ${await internalToken(aud, ctx.claims())}`);
      request.headers.set("X-Request-ID", ctx.requestId);
      if (sendClientIp && ctx.ip !== null) request.headers.set("X-Client-IP", ctx.ip);
      else request.headers.delete("X-Client-IP");
      return request;
    },
  };
}

export function identityClient(ctx: ClientContext): IdentityClient {
  const client = createIdentityClient({
    baseUrl: config().IDENTITY_BASE_URL,
    fetch: resilientFetch("identity", ctx.fetchOptions),
  });
  client.use(authMiddleware("identity", ctx, true));
  return client;
}

export function payrollClient(ctx: ClientContext): PayrollClient {
  const client = createPayrollClient({
    baseUrl: config().PAYROLL_BASE_URL,
    fetch: resilientFetch("payroll", ctx.fetchOptions),
  });
  client.use(authMiddleware("payroll", ctx, false));
  return client;
}

/** The fetch for calculate and approve: read timeout UPSTREAM_SLOW_READ_TIMEOUT_MS (§3.4.2). */
export function slowPayrollFetch(ctx: ClientContext) {
  return resilientFetch("payroll", {
    ...ctx.fetchOptions,
    readTimeoutMs: config().UPSTREAM_SLOW_READ_TIMEOUT_MS,
  });
}
