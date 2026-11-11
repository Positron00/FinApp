"use client";

import { useEffect, useState } from "react";

/**
 * Reads `#token=…` from the URL fragment once, keeps it in component state, and strips it with
 * `history.replaceState`, so the token leaves the address bar and history (web LLD §6.2;
 * PAY-103, PAY-105, PAY-NFR-07). It's never put in a query string, a log or telemetry.
 *
 * Returns `undefined` before the first effect runs, then the token or `null`.
 */
export function readFragmentToken(path: string): string | null {
  const hash = window.location.hash.replace(/^#/, "");
  const token = new URLSearchParams(hash).get("token");
  if (hash !== "") window.history.replaceState(null, "", path);
  return token && /^[A-Za-z0-9_-]{20,128}$/.test(token) ? token : token ? "" : null;
}

export function useFragmentToken(path: string): string | null | undefined {
  const [token, setToken] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    // Reading location.hash must wait for the browser; this runs once after mount. The reader
    // strips the hash, so a second run (React StrictMode re-runs effects in dev) would find
    // nothing: keep the first result.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setToken((prev) => (prev !== undefined ? prev : readFragmentToken(path)));
  }, [path]);
  return token;
}
