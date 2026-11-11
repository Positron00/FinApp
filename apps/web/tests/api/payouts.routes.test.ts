import { http, HttpResponse } from "msw";

import { POST as retry } from "@/app/api/pay-runs/[id]/payouts/[payoutId]/retry/route";
import { GET as listPayouts } from "@/app/api/pay-runs/[id]/payouts/route";
import { GET as listPayslips } from "@/app/api/pay-runs/[id]/payslips/route";

import { PAYROLL } from "../helpers/env";
import { attempt, errorBody, payout, PAYOUT_ID, payslip, RUN_ID } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn } from "../helpers/request";
import { t } from "../helpers/state";

setupMsw();

describe("GET /api/pay-runs/[id]/payslips", () => {
  // PAY-303, C8: cursor and limit pass through; Page<Payslip>, pending amounts null.
  it("returns a page of payslips", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs/${RUN_ID}/payslips`, () =>
        HttpResponse.json({
          items: [
            payslip(),
            payslip({
              status: "pending",
              gross_cents: null,
              tax_cents: null,
              retirement_cents: null,
              net_cents: null,
            }),
          ],
          next_cursor: null,
        }),
      ),
    );
    const res = await listPayslips(
      makeReq(`/api/pay-runs/${RUN_ID}/payslips?cursor=c1&limit=100`, { cookie }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as { items: unknown[] }).items).toHaveLength(2);
    expect(Object.fromEntries(new URL(seen[0]!.url).searchParams)).toEqual({
      cursor: "c1",
      limit: "100",
    });
  });
});

describe("GET /api/pay-runs/[id]/payouts", () => {
  // PAY-501, C8: status, cursor and limit pass through; Page<Payout> with attempts.
  it("returns a page of payouts", async () => {
    const cookie = await signIn({ roles: ["payroll_admin"] });
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs/${RUN_ID}/payouts`, () =>
        HttpResponse.json({
          items: [
            payout(),
            payout({
              status: "requested",
              current_attempt: 1,
              attempts: [
                attempt({ status: "needs_operator", final_at: null, bank_reference: null }),
              ],
            }),
          ],
          next_cursor: "n2",
        }),
      ),
    );
    const res = await listPayouts(
      makeReq(`/api/pay-runs/${RUN_ID}/payouts?status=requested&limit=50`, { cookie }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { next_cursor: string };
    expect(body.next_cursor).toBe("n2");
    expect(Object.fromEntries(new URL(seen[0]!.url).searchParams)).toEqual({
      status: "requested",
      limit: "50",
    });
  });

  // PAY-501, PAY-NFR-08, C5: a payout carrying a full bank number fails the strict schema → 500.
  it("refuses a response that leaks a bank account number", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs/${RUN_ID}/payouts`, () =>
        HttpResponse.json({
          items: [payout({ attempts: [{ ...attempt(), bank_account: "000123456789" }] })],
          next_cursor: null,
        }),
      ),
    );
    const res = await listPayouts(
      makeReq(`/api/pay-runs/${RUN_ID}/payouts`, { cookie }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(500);
    const text = await res.text();
    expect(text).not.toContain("000123456789");
    expect(t.logs.events("bff.response_invalid")[0]).toMatchObject({ target: "payroll" });
    expect(t.logs.text()).not.toContain("000123456789");
  });

  // PAY-501: an unknown status filter is a 422.
  it("rejects an unknown status filter", async () => {
    const cookie = await signIn();
    const res = await listPayouts(
      makeReq(`/api/pay-runs/${RUN_ID}/payouts?status=lost`, { cookie }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(422);
  });
});

describe("POST /api/pay-runs/[id]/payouts/[payoutId]/retry", () => {
  const KEY = "0193a000-0000-4000-8000-00000000f002";
  const URL_ = `${PAYROLL}/v1/pay-runs/${RUN_ID}/payouts/${PAYOUT_ID}/retry`;
  const call = (
    cookie: string | undefined,
    headers: Record<string, string> = { "Idempotency-Key": KEY },
    origin?: string | null,
  ) =>
    retry(
      makeReq(`/api/pay-runs/${RUN_ID}/payouts/${PAYOUT_ID}/retry`, {
        method: "POST",
        cookie,
        headers,
        origin,
      }),
      params({ id: RUN_ID, payoutId: PAYOUT_ID }),
    );
  async function errorOf(res: Response) {
    return ((await res.json()) as { error: { code: string; message: string; details?: unknown } })
      .error;
  }

  // PAY-500, C52: approver retries; the key is forwarded unchanged; 200 Payout.
  it("retries with the key forwarded unchanged", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(
      http.post(URL_, () =>
        HttpResponse.json(
          payout({
            status: "requested",
            current_attempt: 2,
            attempts: [
              attempt({
                status: "rejected",
                failure_reason: "account_closed",
                bank_reference: null,
              }),
              attempt({ attempt: 2, status: "requested", bank_reference: null, final_at: null }),
            ],
          }),
        ),
      ),
    );
    const res = await call(cookie);
    expect(res.status).toBe(200);
    expect(seen[0]!.headers.get("idempotency-key")).toBe(KEY);
    expect(((await res.json()) as { current_attempt: number }).current_attempt).toBe(2);
  });

  // PAY-500, C34: a missing key → 400; a non-UUID → 422 at header.Idempotency-Key.
  it("requires a UUID Idempotency-Key", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    const missing = await call(cookie, {});
    expect(missing.status).toBe(400);
    expect((await errorOf(missing)).code).toBe("idempotency_key_required");
    const bad = await call(cookie, { "Idempotency-Key": "not-a-uuid" });
    expect(bad.status).toBe(422);
    expect((await errorOf(bad)).details).toEqual([
      { field: "header.Idempotency-Key", issue: "invalid_format" },
    ]);
    expect(seen).toHaveLength(0);
  });

  // PAY-500, PAY-601: approver only; Origin; session.
  it("enforces role, Origin and session", async () => {
    const pa = await signIn({ roles: ["payroll_admin", "admin"] });
    expect((await errorOf(await call(pa))).code).toBe("forbidden_role");
    const ap = await signIn({ roles: ["approver"] });
    expect((await errorOf(await call(ap, undefined, "http://evil"))).code).toBe("bad_origin");
    expect((await call(undefined)).status).toBe(401);
    expect(seen).toHaveLength(0);
  });

  // PAY-500: never retried by the BFF (one upstream call on 503).
  it("never retries a 503", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(http.post(URL_, () => HttpResponse.json(errorBody("db_timeout"), { status: 503 })));
    expect((await call(cookie)).status).toBe(503);
    expect(seen).toHaveLength(1);
  });

  // PAY-500, PAY-401: route-specific codes with the retry messages.
  it.each([
    [
      "payout_not_retryable",
      409,
      "Only failed payouts can be retried. Payouts awaiting the bank or blocked can't be retried.",
    ],
    ["retry_limit_reached", 409, "This payout has reached the limit of 5 attempts."],
    [
      "employee_bank_details_missing",
      409,
      "This employee is inactive or has no bank details. Update the employee first.",
    ],
    [
      "approver_must_differ",
      403,
      "Dual approval is on: someone else must retry. You marked this payment rejected, or changed this employee's bank details and a second person hasn't released them yet.",
    ],
    ["payout_not_found", 404, "That payout wasn't found."],
    ["pay_run_not_found", 404, "That pay run wasn't found."],
  ])("maps %s", async (code, status, message) => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(http.post(URL_, () => HttpResponse.json(errorBody(code), { status })));
    const res = await call(cookie);
    expect(res.status).toBe(status);
    const e = await errorOf(res);
    expect(e).toMatchObject({ code, message });
    expect(JSON.stringify(e)).not.toContain("SELECT");
  });
});
