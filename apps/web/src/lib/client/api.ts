/**
 * Browser-side fetch to the BFF (web LLD §1.3, §6.1). Same-origin only. Parses the PAY-602
 * error body into an ApiError and sends the user to `/login?next=` on a 401.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null,
    readonly details: unknown,
    readonly retryAfter: number | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ApiFetchOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  headers?: Record<string, string>;
  /** Don't redirect on 401 (the login form shows `invalid_credentials` itself). */
  noAuthRedirect?: boolean;
}

/** Sends the browser to sign in, keeping the current path (never the fragment). */
export function redirectToLogin(): void {
  const next = `${window.location.pathname}${window.location.search}`;
  // A full navigation on purpose: apiFetch runs outside React, where there's no router.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

export async function apiFetch<T>(path: string, opts: ApiFetchOptions = {}): Promise<T> {
  if (!path.startsWith("/api/")) throw new Error("apiFetch is for /api/* paths only");
  const headers: Record<string, string> = { Accept: "application/json", ...opts.headers };
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, {
    method: opts.method ?? "GET",
    headers,
    body,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (res.ok) {
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }
  let code = "request_failed";
  let message = "The request couldn't be completed.";
  let requestId = res.headers.get("x-request-id");
  let details: unknown = undefined;
  try {
    const b = (await res.json()) as {
      error?: { code?: string; message?: string; request_id?: string; details?: unknown };
    };
    code = b.error?.code ?? code;
    message = b.error?.message ?? message;
    requestId = b.error?.request_id ?? requestId;
    details = b.error?.details;
  } catch {
    // not JSON: keep the generic message
  }
  const ra = Number(res.headers.get("retry-after"));
  const err = new ApiError(
    res.status,
    code,
    message,
    requestId,
    details,
    Number.isFinite(ra) && ra > 0 ? ra : null,
  );
  if (res.status === 401 && code === "unauthenticated" && !opts.noAuthRedirect) redirectToLogin();
  throw err;
}
