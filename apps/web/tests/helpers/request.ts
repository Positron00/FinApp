import { NextRequest } from "next/server";

import { createSession, type NewSession } from "@/lib/session";

import { COMPANY_ID, ORIGIN } from "./env";

/**
 * Builds a NextRequest for a route handler called as a function (web LLD §9.1): cookie,
 * Origin, JSON body, client IP and the params Promise.
 */
export interface ReqOptions {
  method?: string;
  body?: unknown;
  rawBody?: string;
  cookie?: string;
  origin?: string | null;
  headers?: Record<string, string>;
  xff?: string;
}

export function makeReq(path: string, o: ReqOptions = {}): NextRequest {
  const method = o.method ?? "GET";
  const headers = new Headers(o.headers);
  const mutation = !["GET", "HEAD"].includes(method);
  if (o.origin !== null && (o.origin !== undefined || mutation))
    headers.set("origin", o.origin ?? ORIGIN);
  if (o.cookie) headers.set("cookie", `sid=${o.cookie}`);
  if (o.xff) headers.set("x-forwarded-for", o.xff);
  let body: string | undefined;
  if (o.rawBody !== undefined) body = o.rawBody;
  else if (o.body !== undefined) {
    body = JSON.stringify(o.body);
    if (!headers.has("content-type")) headers.set("content-type", "application/json");
  }
  return new NextRequest(new URL(path, ORIGIN), { method, headers, body });
}

export function params(p: Record<string, string> = {}) {
  return { params: Promise.resolve(p) };
}

export const USER_ID = "0193a000-0000-7000-8000-0000000000a1";
export const OTHER_USER_ID = "0193a000-0000-7000-8000-0000000000a2";

/** Creates a session in the fake Redis and returns its cookie value. */
export async function signIn(over: Partial<NewSession> = {}): Promise<string> {
  return createSession(
    {
      user_id: USER_ID,
      company_id: COMPANY_ID,
      name: "Olive Owner",
      roles: ["admin", "payroll_admin", "approver"],
      roles_version: 1,
      ...over,
    },
    "login",
  );
}
