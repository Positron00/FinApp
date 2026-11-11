import path from "node:path";

import { test as base, expect, type Page } from "@playwright/test";

import { authDir } from "./helpers/env";

/**
 * Shared fixtures (web LLD §9.2):
 * - `asOwner` / `asApprover`: pages in separate browser contexts from the global setup's
 *   storageState files;
 * - every page fails its test on a CSP violation (ADR 0009);
 * - `apiFetch(page, path, init)` runs `fetch` inside the page, so cookies and Origin behave as in
 *   the browser (testing.md §6).
 */
export function watchCsp(page: Page, violations: string[]): void {
  page.on("console", (msg) => {
    if (msg.text().includes("Content Security Policy")) violations.push(msg.text());
  });
  void page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) => {
      console.error(`Content Security Policy violation: ${e.violatedDirective} ${e.blockedURI}`);
    });
  });
}

interface Fixtures {
  cspViolations: string[];
  asOwner: Page;
  asApprover: Page;
}

export const test = base.extend<Fixtures>({
  cspViolations: [
    async ({}, provide) => {
      const v: string[] = [];
      await provide(v);
      expect(v, "CSP violations").toEqual([]);
    },
    { auto: true },
  ],
  page: async ({ page, cspViolations }, provide) => {
    watchCsp(page, cspViolations);
    await provide(page);
  },
  asOwner: async ({ browser, cspViolations }, provide) => {
    const ctx = await browser.newContext({ storageState: path.join(authDir(), "owner.json") });
    const page = await ctx.newPage();
    watchCsp(page, cspViolations);
    await provide(page);
    await ctx.close();
  },
  asApprover: async ({ browser, cspViolations }, provide) => {
    const ctx = await browser.newContext({ storageState: path.join(authDir(), "approver.json") });
    const page = await ctx.newPage();
    watchCsp(page, cspViolations);
    await provide(page);
    await ctx.close();
  },
});

export { expect };

export interface InPageResponse {
  status: number;
  headers: Record<string, string>;
  body: unknown;
}

/** `fetch` from inside the page (same-origin cookies and Origin). */
export async function apiFetch(
  page: Page,
  urlPath: string,
  init: { method?: string; headers?: Record<string, string>; body?: unknown } = {},
): Promise<InPageResponse> {
  // fetch needs a document on the app's origin; a fresh page is on about:blank.
  if (!page.url().startsWith("http")) await page.goto("/api/livez");
  return page.evaluate(
    async ({ urlPath, init }) => {
      const res = await fetch(urlPath, {
        method: init.method ?? "GET",
        headers: {
          Accept: "application/json",
          ...(init.body !== undefined ? { "Content-Type": "application/json" } : {}),
          ...init.headers,
        },
        body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
        credentials: "same-origin",
      });
      const headers: Record<string, string> = {};
      res.headers.forEach((v, k) => {
        headers[k] = v;
      });
      const text = await res.text();
      let body: unknown = text;
      try {
        body = JSON.parse(text);
      } catch {
        // not JSON
      }
      return { status: res.status, headers, body };
    },
    { urlPath, init },
  );
}
