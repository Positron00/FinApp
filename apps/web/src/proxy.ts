import { NextResponse, type NextRequest } from "next/server";

import { buildCsp, newNonce } from "./lib/csp";
import { REQUEST_ID_RE, uuidv7 } from "./lib/ids";

/**
 * The request interceptor (web LLD §5.5, ADR 0009). It is NOT a security boundary
 * (architecture.md §9, CVE-2025-29927): for pages it only checks that the session cookie is
 * present (redirecting to /login otherwise); it sets the CSP nonce and the request ID. Every route handler and page calls requireSession() itself
 * (PAY-601). No Redis and no config module here: it reads the few settings it needs directly.
 */

/** Pages that open without a session cookie. `/api/*` is never gated here (C63). */
const OPEN_PAGES = new Set(["/login", "/accept", "/reset"]);

export function proxy(req: NextRequest): NextResponse {
  const env = process.env.ENV ?? "dev";
  const cookieName = process.env.SESSION_COOKIE_NAME || "sid";
  const publicBaseUrl = process.env.PUBLIC_BASE_URL ?? "";

  const incoming = req.headers.get("x-request-id");
  const requestId = incoming && REQUEST_ID_RE.test(incoming) ? incoming : uuidv7();
  const nonce = newNonce();
  const csp = buildCsp(nonce, env, publicBaseUrl);
  const { pathname, search } = req.nextUrl;
  // `/api/*` always passes: each route handler runs requireSession and answers 401 itself,
  // with x-request-id, x-trace-id and Cache-Control (PAY-604), and unknown paths get 404 (C63).
  const gated = !pathname.startsWith("/api/") && !OPEN_PAGES.has(pathname);

  let res: NextResponse;
  if (gated && !req.cookies.has(cookieName)) {
    const login = new URL("/login", req.nextUrl.origin);
    login.searchParams.set("next", `${pathname}${search}`);
    res = NextResponse.redirect(login, 307);
  } else {
    const headers = new Headers(req.headers);
    headers.set("x-nonce", nonce);
    headers.set("x-request-id", requestId);
    headers.set("x-pathname", `${pathname}${search}`);
    headers.set("Content-Security-Policy", csp);
    res = NextResponse.next({ request: { headers } });
  }
  res.headers.set("Content-Security-Policy", csp);
  res.headers.set("x-request-id", requestId);
  if (env === "prod") {
    res.headers.set("Strict-Transport-Security", "max-age=31536000; includeSubDomains");
  }
  return res;
}

export const config = {
  matcher: [
    {
      source: "/((?!_next/static|_next/image|favicon.ico|robots.txt).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
