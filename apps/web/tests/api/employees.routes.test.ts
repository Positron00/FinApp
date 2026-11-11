import { decodeJwt } from "jose";
import { http, HttpResponse } from "msw";

import { GET as getEmployee, PATCH as patchEmployee } from "@/app/api/employees/[id]/route";
import { GET as listEmployees, POST as createEmployee } from "@/app/api/employees/route";

import { PAYROLL } from "../helpers/env";
import { employee, EMPLOYEE_ID, errorBody } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn, USER_ID } from "../helpers/request";
import { t } from "../helpers/state";

setupMsw();

async function errorOf(res: Response) {
  return ((await res.json()) as { error: { code: string; message: string; details?: unknown } })
    .error;
}

const ROUTING = "021000021"; // valid ABA checksum
const ACCOUNT = "123456789012";
const SALARIED = {
  name: "Ada Employee",
  email: "Ada@PayRun.example.com",
  pay_type: "salaried",
  annual_salary_cents: 6_000_000,
  schedule: "monthly",
  bank_routing: ROUTING,
  bank_account: ACCOUNT,
};
const HOURLY = {
  name: "Hal Hourly",
  email: "hal@payrun.example.com",
  pay_type: "hourly",
  hourly_rate_cents: 2500,
  schedule: "biweekly",
  bank_routing: ROUTING,
  bank_account: ACCOUNT,
};

const create = (cookie: string, body: unknown) =>
  createEmployee(makeReq("/api/employees", { method: "POST", cookie, body }), params());
const patch = (cookie: string, body: unknown, id = EMPLOYEE_ID) =>
  patchEmployee(makeReq(`/api/employees/${id}`, { method: "PATCH", cookie, body }), params({ id }));

describe("GET /api/employees", () => {
  // PAY-202, C8: any role; status, cursor and limit pass through; aud=payroll with name.
  it("lists employees with pass-through", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(
      http.get(`${PAYROLL}/v1/employees`, () =>
        HttpResponse.json({
          items: [employee({ account_last4: "6789", has_bank_details: true })],
          next_cursor: null,
        }),
      ),
    );
    const res = await listEmployees(
      makeReq("/api/employees?status=inactive&cursor=c1&limit=50", { cookie }),
      params(),
    );
    expect(res.status).toBe(200);
    expect(Object.fromEntries(new URL(seen[0]!.url).searchParams)).toEqual({
      status: "inactive",
      cursor: "c1",
      limit: "50",
    });
    const claims = decodeJwt(seen[0]!.headers.get("authorization")!.slice(7));
    expect(claims).toMatchObject({ aud: "payroll", sub: USER_ID, name: "Olive Owner" });
  });

  // PAY-202, PAY-NFR-08: a response with a full bank number fails the strict schema → 500.
  it("refuses a response that leaks a bank account number", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${PAYROLL}/v1/employees`, () =>
        HttpResponse.json({
          items: [employee({ bank_account: "998877665544" })],
          next_cursor: null,
        }),
      ),
    );
    const res = await listEmployees(makeReq("/api/employees", { cookie }), params());
    expect(res.status).toBe(500);
    const text = await res.text();
    expect(text).toContain("internal_error");
    expect(text).not.toContain("998877665544");
    expect(t.logs.events("bff.response_invalid")[0]).toMatchObject({ target: "payroll" });
    expect(t.logs.text()).not.toContain("998877665544");
  });

  // PAY-202: an unknown status filter → 422.
  it("rejects an unknown status filter", async () => {
    const cookie = await signIn();
    const res = await listEmployees(makeReq("/api/employees?status=gone", { cookie }), params());
    expect(res.status).toBe(422);
    expect(seen).toHaveLength(0);
  });

  // PAY-106: 401 without a session.
  it("is 401 without a session", async () => {
    expect((await listEmployees(makeReq("/api/employees"), params())).status).toBe(401);
  });
});

describe("POST /api/employees", () => {
  // PAY-201: payroll_admin creates salaried and hourly employees.
  it.each([
    ["salaried", SALARIED],
    ["hourly", HOURLY],
  ])("creates a %s employee", async (_n, body) => {
    const cookie = await signIn({ roles: ["payroll_admin"] });
    server.use(
      http.post(`${PAYROLL}/v1/employees`, () =>
        HttpResponse.json(employee({ account_last4: "9012", has_bank_details: true }), {
          status: 201,
        }),
      ),
    );
    const res = await create(cookie, body);
    expect(res.status).toBe(201);
    const sent = (await seen[0]!.json()) as Record<string, unknown>;
    expect(sent.email).toBe(body.email.toLowerCase());
    expect(sent.pay_type).toBe(body.pay_type);
    // the response never carries the full number
    expect(JSON.stringify(await res.json())).not.toContain(ACCOUNT);
  });

  // PAY-601: only payroll_admin.
  it("is 403 for an admin + approver without payroll_admin", async () => {
    const cookie = await signIn({ roles: ["admin", "approver"] });
    expect((await create(cookie, SALARIED)).status).toBe(403);
    expect(seen).toHaveLength(0);
  });

  // PAY-201, C14: validation without echoing bank numbers.
  it.each([
    [{ ...SALARIED, bank_routing: "021000022" }, "body.bank_routing"],
    [{ ...SALARIED, bank_routing: "12345" }, "body.bank_routing"],
    [{ ...SALARIED, bank_account: "12a4" }, "body.bank_account"],
    [{ ...SALARIED, annual_salary_cents: undefined }, "body.annual_salary_cents"],
    [{ ...SALARIED, hourly_rate_cents: 100 }, "body"],
    [{ ...HOURLY, hourly_rate_cents: 100_001 }, "body.hourly_rate_cents"],
    [{ ...SALARIED, pay_type: "contract" }, "body.pay_type"],
  ])("rejects an invalid body (%#)", async (body, field) => {
    const cookie = await signIn();
    const res = await create(cookie, body);
    expect(res.status).toBe(422);
    const e = await errorOf(res);
    expect((e.details as { field: string }[])[0]!.field).toBe(field);
    expect(JSON.stringify(e)).not.toContain("02100002");
    expect(seen).toHaveLength(0);
  });

  // PAY-201: employee_email_taken.
  it("maps 409 employee_email_taken", async () => {
    const cookie = await signIn();
    server.use(
      http.post(`${PAYROLL}/v1/employees`, () =>
        HttpResponse.json(errorBody("employee_email_taken"), { status: 409 }),
      ),
    );
    const res = await create(cookie, SALARIED);
    expect(res.status).toBe(409);
    expect(await errorOf(res)).toMatchObject({
      code: "employee_email_taken",
      message: "An employee with this email already exists.",
    });
  });

  // PAY-604: Origin.
  it("is 403 bad_origin without an Origin", async () => {
    const cookie = await signIn();
    const res = await createEmployee(
      makeReq("/api/employees", { method: "POST", cookie, body: SALARIED, origin: null }),
      params(),
    );
    expect((await errorOf(res)).code).toBe("bad_origin");
  });
});

describe("GET /api/employees/[id]", () => {
  // PAY-202: any role reads one employee; last 4 only.
  it("returns the employee", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(
      http.get(`${PAYROLL}/v1/employees/${EMPLOYEE_ID}`, () =>
        HttpResponse.json(employee({ account_last4: "6789", has_bank_details: true })),
      ),
    );
    const res = await getEmployee(
      makeReq(`/api/employees/${EMPLOYEE_ID}`, { cookie }),
      params({ id: EMPLOYEE_ID }),
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as { account_last4: string }).account_last4).toBe("6789");
  });

  // PAY-202: employee_not_found; a malformed id → 422.
  it("maps 404 employee_not_found and rejects a bad id", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${PAYROLL}/v1/employees/${EMPLOYEE_ID}`, () =>
        HttpResponse.json(errorBody("employee_not_found"), { status: 404 }),
      ),
    );
    const res = await getEmployee(
      makeReq(`/api/employees/${EMPLOYEE_ID}`, { cookie }),
      params({ id: EMPLOYEE_ID }),
    );
    expect(await errorOf(res)).toMatchObject({
      code: "employee_not_found",
      message: "That employee wasn't found.",
    });
    const bad = await getEmployee(makeReq("/api/employees/x", { cookie }), params({ id: "x" }));
    expect(bad.status).toBe(422);
  });
});

describe("PATCH /api/employees/[id]", () => {
  // PAY-203: payroll_admin updates; the body is forwarded unchanged.
  it("updates an employee", async () => {
    const cookie = await signIn({ roles: ["payroll_admin"] });
    server.use(
      http.patch(`${PAYROLL}/v1/employees/${EMPLOYEE_ID}`, () =>
        HttpResponse.json(employee({ status: "inactive", deactivated_at: "2026-11-10T12:00:00Z" })),
      ),
    );
    const res = await patch(cookie, { status: "inactive" });
    expect(res.status).toBe(200);
    expect(await seen[0]!.json()).toEqual({ status: "inactive" });
  });

  // PAY-203, PAY-608: reactivation with new bank details goes in one PATCH.
  it("forwards reactivation with new bank details", async () => {
    const cookie = await signIn();
    server.use(
      http.patch(`${PAYROLL}/v1/employees/${EMPLOYEE_ID}`, () =>
        HttpResponse.json(employee({ account_last4: "9012", has_bank_details: true })),
      ),
    );
    const res = await patch(cookie, {
      status: "active",
      bank_routing: ROUTING,
      bank_account: ACCOUNT,
    });
    expect(res.status).toBe(200);
    expect(await seen[0]!.json()).toEqual({
      status: "active",
      bank_routing: ROUTING,
      bank_account: ACCOUNT,
    });
  });

  // PAY-203: payroll's required_on_reactivation 422 details are forwarded.
  it("forwards payroll's reactivation 422 details", async () => {
    const cookie = await signIn();
    server.use(
      http.patch(`${PAYROLL}/v1/employees/${EMPLOYEE_ID}`, () =>
        HttpResponse.json(
          errorBody("validation_error", [
            { field: "body.bank_routing", issue: "required_on_reactivation" },
            { field: "body.bank_account", issue: "required_on_reactivation" },
          ]),
          { status: 422 },
        ),
      ),
    );
    const res = await patch(cookie, { status: "active" });
    expect(res.status).toBe(422);
    expect((await errorOf(res)).details).toEqual([
      { field: "body.bank_routing", issue: "required_on_reactivation" },
      { field: "body.bank_account", issue: "required_on_reactivation" },
    ]);
  });

  // PAY-203, C14: an empty body, a null or an unknown key → 422.
  it.each([[{}], [{ name: null }], [{ bank_last4: "1234" }], [{ status: "deleted" }]])(
    "rejects %j",
    async (body) => {
      const cookie = await signIn();
      expect((await patch(cookie, body)).status).toBe(422);
      expect(seen).toHaveLength(0);
    },
  );

  // PAY-203: employee_not_found, employee_email_taken.
  it.each([
    ["employee_not_found", 404],
    ["employee_email_taken", 409],
  ])("maps %s", async (code, status) => {
    const cookie = await signIn();
    server.use(
      http.patch(`${PAYROLL}/v1/employees/${EMPLOYEE_ID}`, () =>
        HttpResponse.json(errorBody(code), { status }),
      ),
    );
    const res = await patch(cookie, { email: "x@y.co" });
    expect(res.status).toBe(status);
    expect((await errorOf(res)).code).toBe(code);
  });

  // PAY-601, PAY-604: role and Origin.
  it("is 403 for an approver and 403 bad_origin cross-site", async () => {
    const approver = await signIn({ roles: ["approver"] });
    expect((await patch(approver, { name: "X" })).status).toBe(403);
    const pa = await signIn({ roles: ["payroll_admin"] });
    const res = await patchEmployee(
      makeReq(`/api/employees/${EMPLOYEE_ID}`, {
        method: "PATCH",
        cookie: pa,
        body: { name: "X" },
        origin: "http://127.0.0.1:9999",
      }),
      params({ id: EMPLOYEE_ID }),
    );
    expect((await errorOf(res)).code).toBe("bad_origin");
    expect(seen).toHaveLength(0);
  });
});
