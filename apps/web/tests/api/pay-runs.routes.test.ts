import { decodeJwt } from "jose";
import { delay, http, HttpResponse } from "msw";

import { POST as approve } from "@/app/api/pay-runs/[id]/approve/route";
import { POST as calculate } from "@/app/api/pay-runs/[id]/calculate/route";
import { GET as getRun } from "@/app/api/pay-runs/[id]/route";
import { GET as listRuns, POST as createRun } from "@/app/api/pay-runs/route";

import { applyEnv, COMPANY_ID, PAYROLL, TEST_ENV } from "../helpers/env";
import { errorBody, payRun, RUN_ID } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn, USER_ID } from "../helpers/request";
import { t } from "../helpers/state";

setupMsw();

const KEY = "0193a000-0000-4000-8000-00000000f001";

async function errorOf(res: Response) {
  return ((await res.json()) as { error: { code: string; message: string; details?: unknown } })
    .error;
}

describe("GET /api/pay-runs", () => {
  // PAY-303, C8: any role; status, cursor and limit pass through; Page<PayRun> returned.
  it("lists runs with query pass-through and a user token for payroll", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs`, () =>
        HttpResponse.json({ items: [payRun()], next_cursor: "abc_DEF-1" }),
      ),
    );
    const res = await listRuns(
      makeReq("/api/pay-runs?status=paid&cursor=abc_DEF-0&limit=10", { cookie }),
      params(),
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { items: unknown[]; next_cursor: string };
    expect(body.items).toHaveLength(1);
    expect(body.next_cursor).toBe("abc_DEF-1");
    const url = new URL(seen[0]!.url);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      status: "paid",
      cursor: "abc_DEF-0",
      limit: "10",
    });
    const claims = decodeJwt(seen[0]!.headers.get("authorization")!.slice(7));
    // PAY-600, C10: user token, aud=payroll, with the name claim
    expect(claims).toMatchObject({
      iss: "web",
      aud: "payroll",
      sub: USER_ID,
      name: "Olive Owner",
      roles: ["approver"],
      cid: COMPANY_ID,
    });
    expect(claims.exp! - claims.iat!).toBe(60);
    expect(seen[0]!.headers.has("x-client-ip")).toBe(false); // only identity gets the client IP
  });

  // PAY-602, C14: a bad query → 422 with query.<name> fields.
  it("rejects a bad limit with 422", async () => {
    const cookie = await signIn();
    const res = await listRuns(makeReq("/api/pay-runs?limit=500", { cookie }), params());
    expect(res.status).toBe(422);
    expect((await errorOf(res)).details).toEqual([{ field: "query.limit", issue: "too_big" }]);
  });

  // PAY-106, PAY-601: no session → 401 unauthenticated.
  it("is 401 without a session", async () => {
    const res = await listRuns(makeReq("/api/pay-runs"), params());
    expect(res.status).toBe(401);
    expect((await errorOf(res)).code).toBe("unauthenticated");
  });

  // PAY-603: payroll down after retries → 503 service_unavailable (GET retried on 503).
  it("retries GET on 503 and then returns 503", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs`, () =>
        HttpResponse.json(errorBody("db_timeout"), { status: 503 }),
      ),
    );
    const res = await listRuns(makeReq("/api/pay-runs", { cookie }), params());
    expect(res.status).toBe(503);
    expect((await errorOf(res)).code).toBe("service_unavailable");
    expect(seen).toHaveLength(3);
  });
});

describe("POST /api/pay-runs", () => {
  // PAY-300, PAY-601: payroll_admin only.
  it("is 403 forbidden_role for an approver-only user", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    const res = await createRun(
      makeReq("/api/pay-runs", {
        method: "POST",
        cookie,
        body: { schedule: "monthly", period_start: "2026-11-01" },
      }),
      params(),
    );
    expect(res.status).toBe(403);
    expect((await errorOf(res)).code).toBe("forbidden_role");
    expect(seen).toHaveLength(0);
  });

  // PAY-300: 201 PayRun.
  it("creates a run", async () => {
    const cookie = await signIn({ roles: ["payroll_admin"] });
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs`, () =>
        HttpResponse.json(payRun({ status: "draft" }), { status: 201 }),
      ),
    );
    const res = await createRun(
      makeReq("/api/pay-runs", {
        method: "POST",
        cookie,
        body: { schedule: "monthly", period_start: "2026-11-01" },
      }),
      params(),
    );
    expect(res.status).toBe(201);
    expect(await seen[0]!.json()).toEqual({ schedule: "monthly", period_start: "2026-11-01" });
  });

  // PAY-300: route-specific errors keep status and code with catalogue messages.
  it.each([
    ["pay_run_exists", 409, "A pay run already exists for this schedule and period."],
    ["invalid_period_start", 422, "Monthly runs must start on the 1st of a month."],
    ["no_employees", 409, "No active employees are on this schedule."],
  ])("maps %s", async (code, status, message) => {
    const cookie = await signIn();
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs`, () => HttpResponse.json(errorBody(code), { status })),
    );
    const res = await createRun(
      makeReq("/api/pay-runs", {
        method: "POST",
        cookie,
        body: { schedule: "monthly", period_start: "2026-11-02" },
      }),
      params(),
    );
    expect(res.status).toBe(status);
    expect(await errorOf(res)).toMatchObject({ code, message });
  });

  // PAY-300, C15: employee_in_overlapping_run forwards details.employee_ids only.
  it("forwards employee_ids for employee_in_overlapping_run", async () => {
    const cookie = await signIn();
    const ids = ["0193a000-0000-7000-8000-0000000000e1", "0193a000-0000-7000-8000-0000000000e2"];
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs`, () =>
        HttpResponse.json(
          errorBody("employee_in_overlapping_run", { employee_ids: ids, names: ["Ada"] }),
          { status: 409 },
        ),
      ),
    );
    const res = await createRun(
      makeReq("/api/pay-runs", {
        method: "POST",
        cookie,
        body: { schedule: "monthly", period_start: "2026-11-01" },
      }),
      params(),
    );
    const err = await errorOf(res);
    expect(err.details).toEqual({ employee_ids: ids });
    expect(err.message).toBe("2 employee(s) are already in another run that covers these dates.");
  });

  // PAY-604: mutations are never retried.
  it("never retries a POST", async () => {
    const cookie = await signIn();
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs`, () =>
        HttpResponse.json(errorBody("db_timeout"), { status: 503 }),
      ),
    );
    const res = await createRun(
      makeReq("/api/pay-runs", {
        method: "POST",
        cookie,
        body: { schedule: "monthly", period_start: "2026-11-01" },
      }),
      params(),
    );
    expect(res.status).toBe(503);
    expect(seen).toHaveLength(1);
  });

  // PAY-604: an Origin check on every mutation.
  it("refuses a missing Origin", async () => {
    const cookie = await signIn();
    const res = await createRun(
      makeReq("/api/pay-runs", {
        method: "POST",
        cookie,
        origin: null,
        body: { schedule: "monthly", period_start: "2026-11-01" },
      }),
      params(),
    );
    expect(res.status).toBe(403);
    expect(t.logs.events("security.bad_origin")).toHaveLength(1);
  });
});

describe("GET /api/pay-runs/[id]", () => {
  // PAY-303: a malformed id is a 422.
  it("rejects a non-UUID id", async () => {
    const cookie = await signIn();
    const res = await getRun(makeReq("/api/pay-runs/nope", { cookie }), params({ id: "nope" }));
    expect(res.status).toBe(422);
    expect((await errorOf(res)).details).toEqual([{ field: "path.id", issue: "invalid_format" }]);
  });

  // PAY-303, PAY-413: the run with counts.
  it("returns the run", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs/${RUN_ID}`, () =>
        HttpResponse.json(
          payRun({
            status: "processing",
            counts: {
              requested: 2,
              awaiting_bank: 1,
              blocked: 1,
              paid: 3,
              no_transfer: 0,
              failed: 0,
            },
          }),
        ),
      ),
    );
    const res = await getRun(
      makeReq(`/api/pay-runs/${RUN_ID}`, { cookie }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as { counts: { blocked: number } }).counts.blocked).toBe(1);
  });

  // PAY-602: 404 pay_run_not_found keeps its code.
  it("maps pay_run_not_found", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${PAYROLL}/v1/pay-runs/${RUN_ID}`, () =>
        HttpResponse.json(errorBody("pay_run_not_found"), { status: 404 }),
      ),
    );
    const res = await getRun(
      makeReq(`/api/pay-runs/${RUN_ID}`, { cookie }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(404);
    expect(await errorOf(res)).toMatchObject({
      code: "pay_run_not_found",
      message: "That pay run wasn't found.",
    });
  });
});

describe("POST /api/pay-runs/[id]/calculate", () => {
  // PAY-302: payroll_admin only.
  it("is 403 for an approver-only user", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    const res = await calculate(
      makeReq(`/api/pay-runs/${RUN_ID}/calculate`, { method: "POST", cookie }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(403);
  });

  // PAY-302, PAY-NFR-05: the slow read timeout applies (a 50 ms default would fail).
  it("uses the slow read timeout", async () => {
    applyEnv({
      ...TEST_ENV,
      UPSTREAM_READ_TIMEOUT_MS: "50",
      UPSTREAM_SLOW_READ_TIMEOUT_MS: "2000",
    });
    const { resetConfigForTests } = await import("@/lib/config");
    resetConfigForTests();
    const cookie = await signIn();
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs/${RUN_ID}/calculate`, async () => {
        await delay(150);
        return HttpResponse.json(payRun());
      }),
    );
    const res = await calculate(
      makeReq(`/api/pay-runs/${RUN_ID}/calculate`, { method: "POST", cookie }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(200);
  });

  // plan §4: a read timeout → 503 service_unavailable.
  it("maps a timeout to 503", async () => {
    applyEnv({ ...TEST_ENV, UPSTREAM_SLOW_READ_TIMEOUT_MS: "50" });
    const { resetConfigForTests } = await import("@/lib/config");
    resetConfigForTests();
    const cookie = await signIn();
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs/${RUN_ID}/calculate`, async () => {
        await delay(500);
        return HttpResponse.json(payRun());
      }),
    );
    const res = await calculate(
      makeReq(`/api/pay-runs/${RUN_ID}/calculate`, { method: "POST", cookie }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(503);
    expect(t.logs.events("bff.upstream_error")[0]).toMatchObject({
      error_kind: "timeout",
      target: "payroll",
    });
  });

  // PAY-302, C15: hours_missing forwards employee_ids and counts them in the message.
  it("maps hours_missing", async () => {
    const cookie = await signIn();
    const ids = ["0193a000-0000-7000-8000-0000000000e1"];
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs/${RUN_ID}/calculate`, () =>
        HttpResponse.json(errorBody("hours_missing", { employee_ids: ids }), { status: 422 }),
      ),
    );
    const res = await calculate(
      makeReq(`/api/pay-runs/${RUN_ID}/calculate`, { method: "POST", cookie }),
      params({ id: RUN_ID }),
    );
    expect(await errorOf(res)).toEqual({
      code: "hours_missing",
      message: "Enter hours for every hourly employee before calculating (1 missing).",
      request_id: expect.any(String),
      details: { employee_ids: ids },
    });
  });

  // PAY-604: a body on a body-less route is a 422.
  it("rejects a non-empty body", async () => {
    const cookie = await signIn();
    const res = await calculate(
      makeReq(`/api/pay-runs/${RUN_ID}/calculate`, { method: "POST", cookie, body: { x: 1 } }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(422);
  });
});

describe("POST /api/pay-runs/[id]/approve", () => {
  const approveReq = (
    cookie: string,
    headers: Record<string, string> = { "Idempotency-Key": KEY },
  ) => makeReq(`/api/pay-runs/${RUN_ID}/approve`, { method: "POST", cookie, headers });

  // PAY-400, PAY-601: approver only.
  it("is 403 for a payroll_admin-only user", async () => {
    const cookie = await signIn({ roles: ["payroll_admin"] });
    const res = await approve(approveReq(cookie), params({ id: RUN_ID }));
    expect(res.status).toBe(403);
  });

  // C34: a missing key → 400 idempotency_key_required.
  it("requires Idempotency-Key", async () => {
    const cookie = await signIn();
    const res = await approve(approveReq(cookie, {}), params({ id: RUN_ID }));
    expect(res.status).toBe(400);
    expect((await errorOf(res)).code).toBe("idempotency_key_required");
    expect(seen).toHaveLength(0);
  });

  // C34, C56: a malformed key → 422 with field header.Idempotency-Key.
  it("rejects a non-UUID key", async () => {
    const cookie = await signIn();
    const res = await approve(
      approveReq(cookie, { "Idempotency-Key": "not-a-uuid" }),
      params({ id: RUN_ID }),
    );
    expect(res.status).toBe(422);
    const err = await errorOf(res);
    expect(err.details).toEqual([{ field: "header.Idempotency-Key", issue: "invalid_format" }]);
    expect(JSON.stringify(err)).not.toContain("not-a-uuid");
  });

  // PAY-400: the key is forwarded unchanged; never retried; 200 PayRun.
  it("forwards the key and never retries", async () => {
    const cookie = await signIn();
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs/${RUN_ID}/approve`, () =>
        HttpResponse.json(payRun({ status: "processing" })),
      ),
    );
    const res = await approve(approveReq(cookie), params({ id: RUN_ID }));
    expect(res.status).toBe(200);
    expect(seen).toHaveLength(1);
    expect(seen[0]!.headers.get("idempotency-key")).toBe(KEY);
  });

  it("does not retry a 503", async () => {
    const cookie = await signIn();
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs/${RUN_ID}/approve`, () =>
        HttpResponse.json(errorBody("db_timeout"), { status: 503 }),
      ),
    );
    const res = await approve(approveReq(cookie), params({ id: RUN_ID }));
    expect(res.status).toBe(503);
    expect(seen).toHaveLength(1);
  });

  // PAY-400, PAY-401, R34: the approve-specific messages.
  it.each([
    [
      "pay_run_not_approvable",
      409,
      "This run can't be approved: it has already been approved, or it isn't calculated.",
    ],
    [
      "approver_must_differ",
      403,
      "Dual approval is on: you can't approve this run because you calculated it, or you changed bank details for an employee in it. Ask another approver.",
    ],
    [
      "employee_bank_details_missing",
      409,
      "Some employees in this run have no bank details. Add their bank details, then approve.",
    ],
    [
      "idempotency_key_reused",
      422,
      "This approval was already submitted with different details. Reload and try again.",
    ],
  ])("maps %s", async (code, status, message) => {
    const cookie = await signIn();
    server.use(
      http.post(`${PAYROLL}/v1/pay-runs/${RUN_ID}/approve`, () =>
        HttpResponse.json(errorBody(code), { status }),
      ),
    );
    const res = await approve(approveReq(cookie), params({ id: RUN_ID }));
    expect(res.status).toBe(status);
    expect(await errorOf(res)).toMatchObject({ code, message });
  });
});
