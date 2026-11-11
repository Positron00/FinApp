import { http, HttpResponse } from "msw";

import { PUT as putHours, GET as hoursGet } from "@/app/api/pay-runs/[id]/hours/route";
import { GET as getPayslip } from "@/app/api/pay-runs/[id]/payslips/[payslipId]/route";
import { DELETE as deleteRun } from "@/app/api/pay-runs/[id]/route";

import { PAYROLL } from "../helpers/env";
import { EMPLOYEE_ID, errorBody, hours, payslip, PAYSLIP_ID, RUN_ID } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn } from "../helpers/request";

setupMsw();

async function errorOf(res: Response) {
  return ((await res.json()) as { error: { code: string; message: string; details?: unknown } })
    .error;
}

const E2 = "0193a000-0000-7000-8000-0000000000e2";
const E3 = "0193a000-0000-7000-8000-0000000000e3";

describe("DELETE /api/pay-runs/[id]", () => {
  const del = (cookie?: string, origin?: string | null) =>
    deleteRun(
      makeReq(`/api/pay-runs/${RUN_ID}`, { method: "DELETE", cookie, origin }),
      params({ id: RUN_ID }),
    );

  // PAY-304: payroll_admin deletes; 204 with no body.
  it("deletes a run", async () => {
    const cookie = await signIn({ roles: ["payroll_admin"] });
    server.use(
      http.delete(
        `${PAYROLL}/v1/pay-runs/${RUN_ID}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    );
    const res = await del(cookie);
    expect(res.status).toBe(204);
    expect(await res.text()).toBe("");
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(seen[0]!.method).toBe("DELETE");
  });

  // PAY-304: pay_run_locked, pay_run_not_found.
  it.each([
    ["pay_run_locked", 409, "This run is locked because it has been approved."],
    ["pay_run_not_found", 404, "That pay run wasn't found."],
  ])("maps %s", async (code, status, message) => {
    const cookie = await signIn();
    server.use(
      http.delete(`${PAYROLL}/v1/pay-runs/${RUN_ID}`, () =>
        HttpResponse.json(errorBody(code), { status }),
      ),
    );
    const res = await del(cookie);
    expect(res.status).toBe(status);
    expect(await errorOf(res)).toMatchObject({ code, message });
  });

  // PAY-601, PAY-604, PAY-106: role, Origin, session.
  it("enforces role, Origin and session", async () => {
    const approver = await signIn({ roles: ["approver"] });
    expect((await del(approver)).status).toBe(403);
    const pa = await signIn({ roles: ["payroll_admin"] });
    expect((await errorOf(await del(pa, "http://evil.example"))).code).toBe("bad_origin");
    expect((await del(undefined)).status).toBe(401);
    expect(seen).toHaveLength(0);
  });

  // PAY-602: a DELETE is never retried by the BFF.
  it("never retries a 503", async () => {
    const cookie = await signIn();
    server.use(
      http.delete(`${PAYROLL}/v1/pay-runs/${RUN_ID}`, () =>
        HttpResponse.json(errorBody("db_unavailable"), { status: 503 }),
      ),
    );
    expect((await del(cookie)).status).toBe(503);
    expect(seen).toHaveLength(1);
  });
});

describe("PUT /api/pay-runs/[id]/hours", () => {
  const put = (cookie: string, body: unknown) =>
    putHours(
      makeReq(`/api/pay-runs/${RUN_ID}/hours`, { method: "PUT", cookie, body }),
      params({ id: RUN_ID }),
    );
  const ENTRIES = { entries: [{ employee_id: EMPLOYEE_ID, hours_hundredths: 8050 }] };

  // PAY-301, C7: the BFF returns payroll's HoursOut.
  it("saves hours and returns Hours", async () => {
    const cookie = await signIn({ roles: ["payroll_admin"] });
    server.use(
      http.put(`${PAYROLL}/v1/pay-runs/${RUN_ID}/hours`, () => HttpResponse.json(hours())),
    );
    const res = await put(cookie, ENTRIES);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(hours());
    expect(await seen[0]!.json()).toEqual(ENTRIES);
  });

  // PAY-301, C15: not_hourly and employee_not_in_run forward employee_ids and count them.
  it.each([
    ["not_hourly", "Hours can only be entered for hourly employees."],
    ["employee_not_in_run", "2 employee(s) aren't in this run. Reload the run and try again."],
  ])("maps %s with employee_ids", async (code, message) => {
    const cookie = await signIn();
    server.use(
      http.put(`${PAYROLL}/v1/pay-runs/${RUN_ID}/hours`, () =>
        HttpResponse.json(errorBody(code, { employee_ids: [E2, E3], secret: "x" }), {
          status: 422,
        }),
      ),
    );
    const res = await put(cookie, ENTRIES);
    expect(res.status).toBe(422);
    const e = await errorOf(res);
    expect(e).toMatchObject({ code, message, details: { employee_ids: [E2, E3] } });
    expect(JSON.stringify(e)).not.toContain("secret");
  });

  // PAY-301: pay_run_locked.
  it("maps 409 pay_run_locked", async () => {
    const cookie = await signIn();
    server.use(
      http.put(`${PAYROLL}/v1/pay-runs/${RUN_ID}/hours`, () =>
        HttpResponse.json(errorBody("pay_run_locked"), { status: 409 }),
      ),
    );
    expect((await errorOf(await put(cookie, ENTRIES))).code).toBe("pay_run_locked");
  });

  // PAY-301, C14: duplicates, too many entries, out-of-bound hours, non-integers → 422.
  it.each([
    [
      {
        entries: [
          { employee_id: EMPLOYEE_ID, hours_hundredths: 1 },
          { employee_id: EMPLOYEE_ID, hours_hundredths: 2 },
        ],
      },
      "body.entries",
    ],
    [
      {
        entries: Array.from({ length: 501 }, (_, i) => ({
          employee_id: `0193a000-0000-7000-8000-${String(i).padStart(12, "0")}`,
          hours_hundredths: 1,
        })),
      },
      "body.entries",
    ],
    [{ entries: [] }, "body.entries"],
    [
      { entries: [{ employee_id: EMPLOYEE_ID, hours_hundredths: 74_401 }] },
      "body.entries.0.hours_hundredths",
    ],
    [
      { entries: [{ employee_id: EMPLOYEE_ID, hours_hundredths: 1.5 }] },
      "body.entries.0.hours_hundredths",
    ],
    [{ entries: [{ employee_id: "x", hours_hundredths: 1 }] }, "body.entries.0.employee_id"],
  ])("rejects an invalid body (%#)", async (body, field) => {
    const cookie = await signIn();
    const res = await put(cookie, body);
    expect(res.status).toBe(422);
    expect(((await errorOf(res)).details as { field: string }[])[0]!.field).toBe(field);
    expect(seen).toHaveLength(0);
  });

  // PAY-601, PAY-604.
  it("enforces role and Origin", async () => {
    const approver = await signIn({ roles: ["approver", "admin"] });
    expect((await put(approver, ENTRIES)).status).toBe(403);
    const pa = await signIn({ roles: ["payroll_admin"] });
    const res = await putHours(
      makeReq(`/api/pay-runs/${RUN_ID}/hours`, {
        method: "PUT",
        cookie: pa,
        body: ENTRIES,
        origin: null,
      }),
      params({ id: RUN_ID }),
    );
    expect((await errorOf(res)).code).toBe("bad_origin");
    expect(seen).toHaveLength(0);
  });

  // PAY-602: GET → 405.
  it("is 405 for GET", async () => {
    const res = await hoursGet(makeReq(`/api/pay-runs/${RUN_ID}/hours`), params());
    expect(res.status).toBe(405);
    expect(res.headers.get("allow")).toBe("PUT");
  });
});

describe("GET /api/pay-runs/[id]/payslips/[payslipId]", () => {
  const get = (cookie: string | undefined, payslipId = PAYSLIP_ID) =>
    getPayslip(
      makeReq(`/api/pay-runs/${RUN_ID}/payslips/${payslipId}`, { cookie }),
      params({ id: RUN_ID, payslipId }),
    );

  // PAY-303: any role reads one payslip.
  it("returns the payslip", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs/${RUN_ID}/payslips/${PAYSLIP_ID}`, () =>
        HttpResponse.json(payslip()),
      ),
    );
    const res = await get(cookie);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(payslip());
  });

  // PAY-303: payslip_not_found, pay_run_not_found.
  it.each([
    ["payslip_not_found", "That payslip wasn't found."],
    ["pay_run_not_found", "That pay run wasn't found."],
  ])("maps %s", async (code, message) => {
    const cookie = await signIn();
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs/${RUN_ID}/payslips/${PAYSLIP_ID}`, () =>
        HttpResponse.json(errorBody(code), { status: 404 }),
      ),
    );
    const res = await get(cookie);
    expect(res.status).toBe(404);
    expect(await errorOf(res)).toMatchObject({ code, message });
  });

  // PAY-602, PAY-106: a malformed payslipId → 422; no session → 401.
  it("rejects a malformed payslipId and requires a session", async () => {
    const cookie = await signIn();
    const res = await get(cookie, "nope");
    expect(res.status).toBe(422);
    expect((await errorOf(res)).details).toEqual([
      { field: "path.payslipId", issue: "invalid_format" },
    ]);
    expect((await get(undefined)).status).toBe(401);
    expect(seen).toHaveLength(0);
  });
});
