// Sign in through the BFF (web LLD §9.3). Returns "name=value" for the session cookie.
// TARGET_URL is where requests go (inside the Compose network: http://web:3000); BASE_URL is the
// public origin the BFF checks on mutations (http://127.0.0.1:4100; C12).
import http from "k6/http";

export const TARGET_URL = __ENV.TARGET_URL || __ENV.BASE_URL || "http://127.0.0.1:4100";
export const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:4100";
export const SESSION_COOKIE = __ENV.SESSION_COOKIE || "sid"; // "__Host-sid" in prod-like runs

// Sessions travel only in the explicit Cookie header, never in k6's per-VU cookie jar: every
// request passes a throwaway jar (`noJar()`). With the shared jar, a second login (the approver)
// sent the first user's sid along; the BFF rotates the presented session at sign-in (session
// fixation), so it deleted the first session and the jar then carried the approver's sid on the
// first user's requests (403 forbidden_role, then 401s; the first nightly, 2026-11-11).
export function noJar() {
  return new http.CookieJar();
}

export function login(email, password) {
  const r = http.post(`${TARGET_URL}/api/auth/login`, JSON.stringify({ email, password }), {
    headers: { "Content-Type": "application/json", Origin: BASE_URL },
    tags: { name: "/api/auth/login", kind: "simple" },
    jar: noJar(),
  });
  if (r.status !== 200 || !r.cookies[SESSION_COOKIE]) {
    throw new Error(`login failed: HTTP ${r.status}`);
  }
  return `${SESSION_COOKIE}=${r.cookies[SESSION_COOKIE][0].value}`;
}
