import { randomInt, randomUUID } from "node:crypto";

import {
  expect,
  request as playwrightRequest,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";

import type {
  Employee,
  Me,
  PayRun,
  PayRunStatus,
  Payout,
  Payslip,
  Settings,
  User,
} from "../../src/lib/schemas/responses";
import { apiFetch, watchCsp, type InPageResponse } from "../fixtures";

import { baseUrl } from "./env";
import { latestToken } from "./mailpit";

/**
 * Data set-up for the journeys, through the BFF from a signed-in page (`apiFetch`, so cookies and
 * Origin behave as in the browser). The specs drive the UI for the steps they test and use these
 * helpers for everything else. Every list follows `next_cursor` (C8).
 */
export type { Employee, Me, PayRun, PayRunStatus, Payout, Payslip, Settings, User };
export type Schedule = "monthly" | "biweekly";
export const FINAL: PayRunStatus[] = ["paid", "partially_failed"];
const DAY_MS = 86_400_000;

/** The body of a 2xx, or a thrown error naming the status and error code. */
export function ok<T>(res: InPageResponse, what: string): T {
  if (res.status < 200 || res.status >= 300) {
    const code = (res.body as { error?: { code?: string } } | null)?.error?.code;
    throw new Error(`${what}: HTTP ${res.status} ${code ?? ""}`.trim());
  }
  return res.body as T;
}

export function errorCode(res: InPageResponse): string | undefined {
  return (res.body as { error?: { code?: string } } | null)?.error?.code;
}

/** Every item of a paged list (C8). */
export async function allPages<T>(page: Page, path: string, limit = 100): Promise<T[]> {
  const out: T[] = [];
  let cursor: string | null = null;
  do {
    const sep = path.includes("?") ? "&" : "?";
    const url: string = `${path}${sep}limit=${limit}${cursor ? `&cursor=${cursor}` : ""}`;
    const body: { items: T[]; next_cursor: string | null } = ok(await apiFetch(page, url), url);
    out.push(...body.items);
    cursor = body.next_cursor;
  } while (cursor !== null);
  return out;
}

export async function getSettings(page: Page): Promise<Settings> {
  return ok<Settings>(await apiFetch(page, "/api/settings"), "settings");
}

export async function me(page: Page): Promise<Me> {
  return ok<Me>(await apiFetch(page, "/api/auth/me"), "me");
}

const iso = (ms: number): string => new Date(ms).toISOString().slice(0, 10);

/**
 * A period start no other test uses: monthly runs in a random far-future month, biweekly runs a
 * random number of periods after the anchor. A collision (409 `pay_run_exists`) just retries.
 */
export function randomPeriodStart(schedule: Schedule, anchor: string): string {
  if (schedule === "monthly") {
    return `${2100 + randomInt(800)}-${String(1 + randomInt(12)).padStart(2, "0")}-01`;
  }
  const [y, m, d] = anchor.split("-").map(Number) as [number, number, number];
  return iso(Date.UTC(y, m - 1, d) + (2600 + randomInt(20_000)) * 14 * DAY_MS);
}

export async function createRun(
  page: Page,
  schedule: Schedule,
  periodStart?: string,
): Promise<PayRun> {
  const { biweekly_anchor } = await getSettings(page);
  for (let i = 0; i < 5; i++) {
    const period_start = periodStart ?? randomPeriodStart(schedule, biweekly_anchor);
    const res = await apiFetch(page, "/api/pay-runs", {
      method: "POST",
      body: { schedule, period_start },
    });
    if (res.status === 409 && errorCode(res) === "pay_run_exists" && !periodStart) continue;
    return ok<PayRun>(res, `create ${schedule} run ${period_start}`);
  }
  throw new Error("no free period after 5 tries");
}

/** Hours for every hourly payslip (in PUTs of at most 500, PAY-301). */
export async function enterAllHours(page: Page, runId: string, hundredths = 16_000) {
  const slips = await allPages<Payslip>(page, `/api/pay-runs/${runId}/payslips`);
  const hourly = slips.filter((p) => p.pay_type === "hourly");
  for (let i = 0; i < hourly.length; i += 500) {
    const entries = hourly
      .slice(i, i + 500)
      .map((p) => ({ employee_id: p.employee_id, hours_hundredths: hundredths }));
    ok(
      await apiFetch(page, `/api/pay-runs/${runId}/hours`, {
        method: "PUT",
        body: { entries },
      }),
      "hours",
    );
  }
  return hourly.length;
}

export async function calculate(page: Page, runId: string): Promise<PayRun> {
  return ok<PayRun>(
    await apiFetch(page, `/api/pay-runs/${runId}/calculate`, { method: "POST" }),
    "calculate",
  );
}

/** Create, enter hours and calculate a run as `page`'s user (the preparer). */
export async function prepareRun(page: Page, schedule: Schedule = "biweekly"): Promise<PayRun> {
  const run = await createRun(page, schedule);
  await enterAllHours(page, run.id);
  return calculate(page, run.id);
}

export async function approve(page: Page, runId: string, key = randomUUID()) {
  return apiFetch(page, `/api/pay-runs/${runId}/approve`, {
    method: "POST",
    headers: { "Idempotency-Key": key },
  });
}

export async function getRun(page: Page, runId: string): Promise<PayRun> {
  return ok<PayRun>(await apiFetch(page, `/api/pay-runs/${runId}`), "run");
}

export async function payouts(page: Page, runId: string): Promise<Payout[]> {
  return allPages<Payout>(page, `/api/pay-runs/${runId}/payouts`);
}

/** Polls the run until its status is one of `statuses`. */
export async function waitForRun(
  page: Page,
  runId: string,
  statuses: PayRunStatus[],
  timeout = 90_000,
): Promise<PayRun> {
  let last: PayRun | undefined;
  await expect
    .poll(
      async () => {
        last = await getRun(page, runId);
        return statuses.includes(last.status) ? "done" : last.status;
      },
      { message: `run ${runId} reaches ${statuses.join(" or ")}`, timeout, intervals: [1000] },
    )
    .toBe("done");
  return last as PayRun;
}

let accountSeq = randomInt(1_000_000);
/** A unique account number (12 digits, never ending in 0000 unless asked). */
export function accountNumber(endsWith?: string): string {
  accountSeq += 1;
  const head = `${Date.now() % 1_000_000_000}${String(accountSeq % 1000).padStart(3, "0")}`;
  if (endsWith) return head.slice(0, 12 - endsWith.length) + endsWith;
  return head.endsWith("0000") ? `${head.slice(0, -1)}7` : head;
}

export const ROUTING = "021000021"; // a valid ABA checksum

export function uniqueEmail(prefix = "e2e"): string {
  return `${prefix}+${randomUUID()}@payrun.example.com`;
}

export async function createEmployee(
  page: Page,
  overrides: Partial<Record<string, unknown>> = {},
): Promise<Employee> {
  const body = {
    name: `E2E ${randomUUID().slice(0, 8)}`,
    email: uniqueEmail("e2e-emp"),
    pay_type: "salaried",
    schedule: "biweekly",
    annual_salary_cents: 5_200_000,
    bank_routing: ROUTING,
    bank_account: accountNumber(),
    ...overrides,
  };
  return ok<Employee>(
    await apiFetch(page, "/api/employees", { method: "POST", body }),
    "create employee",
  );
}

/**
 * J4/J5 need an active biweekly employee whose account ends in 0000 (the bank-stub's
 * `account_closed`). The full seed's employee 9 is one until J5 fixes its bank details; when
 * none is left, this creates one.
 */
export async function ensureClosedAccountEmployee(page: Page): Promise<Employee> {
  const all = await allPages<Employee>(page, "/api/employees?status=active");
  const hit = all.find((e) => e.schedule === "biweekly" && e.account_last4 === "0000");
  return hit ?? createEmployee(page, { bank_account: accountNumber("0000") });
}

export interface TestUser {
  id: string;
  email: string;
  name: string;
  password: string;
  roles: string[];
}

/**
 * A new user with `roles`: the admin (`ownerPage`) invites; the invite token comes from Mailpit;
 * the accept goes through the BFF from a separate request context, so the owner's session is
 * untouched (PAY-101, PAY-103).
 */
export async function createUser(ownerPage: Page, roles: string[]): Promise<TestUser> {
  const email = uniqueEmail();
  const name = `E2E User ${randomUUID().slice(0, 6)}`;
  const password = `pw-${randomUUID()}`;
  const invited = ok<User>(
    await apiFetch(ownerPage, "/api/users/invite", {
      method: "POST",
      body: { email, name, roles },
    }),
    "invite",
  );
  const { token } = await latestToken(email, "accept");
  const ctx = await playwrightRequest.newContext({ baseURL: baseUrl() });
  try {
    const res = await ctx.post("/api/auth/accept", {
      data: { token, password },
      headers: { Origin: baseUrl() },
    });
    if (res.status() !== 200) throw new Error(`accept: HTTP ${res.status()}`);
  } finally {
    await ctx.dispose();
  }
  return { id: invited.id, email, name, password, roles };
}

/** A new browser context signed in as `email` (through the login API), with the CSP watch. */
export async function signedInPage(
  browser: Browser,
  email: string,
  password: string,
  cspViolations: string[],
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ baseURL: baseUrl() });
  const res = await context.request.post("/api/auth/login", {
    data: { email, password },
    headers: { Origin: baseUrl() },
  });
  if (res.status() !== 200) throw new Error(`login ${email}: HTTP ${res.status()}`);
  const page = await context.newPage();
  watchCsp(page, cspViolations);
  return { context, page };
}

/** "$1,234.56" or "-$1.00" → cents; "—" → null. */
export function parseCents(text: string): number | null {
  const t = text.trim();
  if (t === "—" || t === "") return null;
  const m = /^(-)?\$([\d,]+)\.(\d{2})$/.exec(t);
  if (!m) throw new Error(`not an amount: ${JSON.stringify(t)}`);
  const cents = Number(m[2]!.replaceAll(",", "")) * 100 + Number(m[3]);
  return m[1] ? -cents : cents;
}
