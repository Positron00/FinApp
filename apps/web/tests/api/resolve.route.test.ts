import { decodeJwt } from "jose";
import { http, HttpResponse } from "msw";

import { POST as resolve } from "@/app/api/pay-runs/[id]/payouts/[payoutId]/resolve/route";

import { PAYROLL } from "../helpers/env";
import { attempt, errorBody, payout, PAYOUT_ID, RUN_ID } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn } from "../helpers/request";
import { t } from "../helpers/state";

setupMsw();

const UPSTREAM = `${PAYROLL}/v1/pay-runs/${RUN_ID}/payouts/${PAYOUT_ID}/resolve`;
const NOTE = "Checked the bank portal: transfer XYZ-77 not found";

async function errorOf(res: Response) {
  return ((await res.json()) as { error: { code: string; message: string; details?: unknown } })
    .error;
}

const call = (cookie: string | undefined, body: unknown, origin?: string | null) =>
  resolve(
    makeReq(`/api/pay-runs/${RUN_ID}/payouts/${PAYOUT_ID}/resolve`, {
      method: "POST",
      cookie,
      body,
      origin,
    }),
    params({ id: RUN_ID, payoutId: PAYOUT_ID }),
  );

describe("POST /api/pay-runs/[id]/payouts/[payoutId]/resolve", () => {
  // PAY-502, R8: {action, note} forwarded unchanged with aud=payroll; 200 Payout.
  it.each(["redrive", "mark_rejected"])("forwards %s and returns the payout", async (action) => {
    const cookie = await signIn({ roles: ["approver"] });
    const result =
      action === "redrive"
        ? payout({
            status: "requested",
            attempts: [attempt({ status: "requested", bank_reference: null, final_at: null })],
          })
        : payout({
            status: "failed",
            attempts: [
              attempt({
                status: "rejected",
                failure_reason: "operator_rejected",
                bank_reference: null,
              }),
            ],
          });
    server.use(http.post(UPSTREAM, () => HttpResponse.json(result)));
    const res = await call(cookie, { action, note: NOTE });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(result);
    expect(await seen[0]!.json()).toEqual({ action, note: NOTE });
    expect(decodeJwt(seen[0]!.headers.get("authorization")!.slice(7)).aud).toBe("payroll");
    // PAY-NFR-07: the note is never logged; only its length.
    expect(t.logs.text()).not.toContain("XYZ-77");
    expect(t.logs.events("bff.payout_resolve")[0]).toMatchObject({
      action,
      note_len: NOTE.length,
      status: 200,
    });
  });

  // PAY-502, PAY-601: approver only (payroll_admin-only gets 403); no session → 401.
  it("is approver only", async () => {
    const pa = await signIn({ roles: ["payroll_admin", "admin"] });
    const res = await call(pa, { action: "redrive", note: NOTE });
    expect(res.status).toBe(403);
    expect((await errorOf(res)).code).toBe("forbidden_role");
    expect((await call(undefined, { action: "redrive", note: NOTE })).status).toBe(401);
    expect(seen).toHaveLength(0);
  });

  // PAY-604: Origin.
  it("is 403 bad_origin cross-site", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    const res = await call(cookie, { action: "redrive", note: NOTE }, "https://evil.example");
    expect((await errorOf(res)).code).toBe("bad_origin");
    expect(seen).toHaveLength(0);
  });

  // PAY-502, C14: unknown action, empty note, 501 characters → 422 with no value echoed.
  it.each([
    [{ action: "delete", note: NOTE }, "body.action"],
    [{ action: "redrive", note: "   " }, "body.note"],
    [{ action: "redrive", note: "z".repeat(501) }, "body.note"],
    [{ action: "redrive" }, "body.note"],
    [{ note: NOTE }, "body.action"],
  ])("rejects %#", async (body, field) => {
    const cookie = await signIn({ roles: ["approver"] });
    const res = await call(cookie, body);
    expect(res.status).toBe(422);
    const e = await errorOf(res);
    expect((e.details as { field: string }[])[0]!.field).toBe(field);
    expect(JSON.stringify(e)).not.toContain("XYZ-77");
    expect(JSON.stringify(e)).not.toContain("zzzz");
    expect(seen).toHaveLength(0);
  });

  // PAY-502: never retried by the BFF.
  it("never retries a 503", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(
      http.post(UPSTREAM, () => HttpResponse.json(errorBody("db_unavailable"), { status: 503 })),
    );
    const res = await call(cookie, { action: "redrive", note: NOTE });
    expect(res.status).toBe(503);
    expect(seen).toHaveLength(1);
    expect(t.logs.events("bff.payout_resolve")[0]).toMatchObject({ status: 503 });
  });

  // PAY-502, C48: resolve_too_early forwards Retry-After and puts the seconds in the message.
  it("maps resolve_too_early with Retry-After", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(
      http.post(UPSTREAM, () =>
        HttpResponse.json(errorBody("resolve_too_early", { retry_after_s: 87 }), {
          status: 409,
          headers: { "Retry-After": "87" },
        }),
      ),
    );
    const res = await call(cookie, { action: "mark_rejected", note: NOTE });
    expect(res.status).toBe(409);
    expect(res.headers.get("retry-after")).toBe("87");
    const e = await errorOf(res);
    expect(e).toMatchObject({
      code: "resolve_too_early",
      message:
        "This payout was blocked moments ago. Wait 87 seconds, check the bank again, then mark it rejected.",
    });
    expect(e.details).toBeUndefined();
    expect(t.logs.events("bff.payout_resolve")[0]).toMatchObject({
      action: "mark_rejected",
      status: 409,
    });
    expect(t.logs.text()).not.toContain("XYZ-77");
  });

  // C73: payroll's 422 for a redrive of a key_window_expired attempt keeps its detail, so the
  // dialog can show the readable message.
  it("forwards 422 redrive_not_allowed with its detail", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(
      http.post(UPSTREAM, () =>
        HttpResponse.json(
          errorBody("validation_error", [{ field: "body.action", issue: "redrive_not_allowed" }]),
          { status: 422 },
        ),
      ),
    );
    const res = await call(cookie, { action: "redrive", note: NOTE });
    expect(res.status).toBe(422);
    expect(await errorOf(res)).toMatchObject({
      code: "validation_error",
      message: "Some fields need attention.",
      details: [{ field: "body.action", issue: "redrive_not_allowed" }],
    });
  });

  // PAY-502: the other route-specific codes.
  it.each([
    ["payout_not_blocked", 409, "This payout isn't blocked any more. Refresh the page."],
    ["pay_run_not_found", 404, "That pay run wasn't found."],
    ["payout_not_found", 404, "That payout wasn't found."],
  ])("maps %s", async (code, status, message) => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(http.post(UPSTREAM, () => HttpResponse.json(errorBody(code), { status })));
    const res = await call(cookie, { action: "redrive", note: NOTE });
    expect(res.status).toBe(status);
    expect(await errorOf(res)).toMatchObject({ code, message });
  });

  // PAY-502, PAY-NFR-08: a payout response with an extra key fails the strict schema.
  it("refuses an invalid payout response", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(http.post(UPSTREAM, () => HttpResponse.json({ ...payout(), routing: "021000021" })));
    const res = await call(cookie, { action: "redrive", note: NOTE });
    expect(res.status).toBe(500);
    expect(await res.text()).not.toContain("021000021");
    expect(t.logs.events("bff.payout_resolve")[0]).toMatchObject({ status: 500 });
  });
});
