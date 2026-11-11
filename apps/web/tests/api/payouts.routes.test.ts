import { http, HttpResponse } from "msw";

import { GET as listPayouts } from "@/app/api/pay-runs/[id]/payouts/route";
import { GET as listPayslips } from "@/app/api/pay-runs/[id]/payslips/route";

import { PAYROLL } from "../helpers/env";
import { attempt, payout, payslip, RUN_ID } from "../helpers/fixtures";
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
