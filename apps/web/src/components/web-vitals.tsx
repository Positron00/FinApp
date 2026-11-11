"use client";

import { useReportWebVitals } from "next/web-vitals";
import { useEffect } from "react";

/**
 * Web Vitals and browser errors to `/api/telemetry` (web LLD §7.5). `route` is a template with
 * IDs replaced, never the real path, the query or the fragment.
 */
export function routeTemplate(pathname: string): string {
  return pathname
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, "[id]")
    .slice(0, 100);
}

export function send(payload: Record<string, unknown>): void {
  const json = JSON.stringify(payload);
  const blob = new Blob([json], { type: "application/json" });
  if (typeof navigator.sendBeacon === "function" && navigator.sendBeacon("/api/telemetry", blob)) {
    return;
  }
  void fetch("/api/telemetry", {
    method: "POST",
    body: json,
    headers: { "Content-Type": "application/json" },
    keepalive: true,
  }).catch(() => undefined);
}

export function WebVitals() {
  useReportWebVitals((m) => {
    send({
      type: "vital",
      name: m.name,
      value: m.value,
      rating: m.rating,
      route: routeTemplate(window.location.pathname),
    });
  });
  useEffect(() => {
    const onError = (e: ErrorEvent) =>
      send({
        type: "error",
        message: String(e.message).slice(0, 500),
        route: routeTemplate(window.location.pathname),
      });
    const onRejection = (e: PromiseRejectionEvent) =>
      send({
        type: "error",
        message: String((e.reason as { message?: unknown } | null)?.message ?? e.reason).slice(
          0,
          500,
        ),
        route: routeTemplate(window.location.pathname),
      });
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);
  return null;
}
