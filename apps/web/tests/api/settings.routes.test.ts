import { decodeJwt } from "jose";
import { http, HttpResponse } from "msw";

import {
  GET as getSettings,
  PUT as putSettings,
  POST as postSettings,
} from "@/app/api/settings/route";

import { PAYROLL } from "../helpers/env";
import { errorBody, settings } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn, USER_ID } from "../helpers/request";

setupMsw();

async function errorOf(res: Response) {
  return ((await res.json()) as { error: { code: string; message: string; details?: unknown } })
    .error;
}

const BODY = {
  name: "Acme Ltd",
  tax_rate_bps: 2000,
  retirement_bps: 500,
  timezone: "America/New_York",
  biweekly_anchor: "2026-01-05", // a Monday
  require_dual_approval: true,
};

const put = (cookie: string, body: unknown) =>
  putSettings(makeReq("/api/settings", { method: "PUT", cookie, body }), params());

describe("GET /api/settings", () => {
  // PAY-200, PAY-600: any role; a user token with aud=payroll and the name claim.
  it("returns the settings to any role", async () => {
    const cookie = await signIn({ roles: ["approver"] });
    server.use(http.get(`${PAYROLL}/v1/company/settings`, () => HttpResponse.json(settings())));
    const res = await getSettings(makeReq("/api/settings", { cookie }), params());
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(settings());
    const claims = decodeJwt(seen[0]!.headers.get("authorization")!.slice(7));
    expect(claims).toMatchObject({ aud: "payroll", sub: USER_ID, name: "Olive Owner" });
  });

  // PAY-200, C15: company_settings_not_found.
  it("maps 404 company_settings_not_found", async () => {
    const cookie = await signIn();
    server.use(
      http.get(`${PAYROLL}/v1/company/settings`, () =>
        HttpResponse.json(errorBody("company_settings_not_found"), { status: 404 }),
      ),
    );
    const res = await getSettings(makeReq("/api/settings", { cookie }), params());
    expect(res.status).toBe(404);
    expect(await errorOf(res)).toMatchObject({
      code: "company_settings_not_found",
      message: "Company settings haven't been set up yet. Ask an admin.",
    });
  });

  // PAY-106: 401 without a session.
  it("is 401 without a session", async () => {
    expect((await getSettings(makeReq("/api/settings"), params())).status).toBe(401);
  });
});

describe("PUT /api/settings", () => {
  // PAY-200: admin saves; the body is forwarded unchanged.
  it("saves the settings", async () => {
    const cookie = await signIn({ roles: ["admin"] });
    server.use(http.put(`${PAYROLL}/v1/company/settings`, () => HttpResponse.json(settings())));
    const res = await put(cookie, BODY);
    expect(res.status).toBe(200);
    expect(await seen[0]!.json()).toEqual(BODY);
  });

  // PAY-601: payroll_admin and approver can't save.
  it("is 403 for non-admins", async () => {
    const cookie = await signIn({ roles: ["payroll_admin", "approver"] });
    const res = await put(cookie, BODY);
    expect(res.status).toBe(403);
    expect(seen).toHaveLength(0);
  });

  // PAY-200, R33: anchor_locked.
  it("maps 409 anchor_locked", async () => {
    const cookie = await signIn();
    server.use(
      http.put(`${PAYROLL}/v1/company/settings`, () =>
        HttpResponse.json(errorBody("anchor_locked"), { status: 409 }),
      ),
    );
    const res = await put(cookie, BODY);
    expect(res.status).toBe(409);
    expect(await errorOf(res)).toMatchObject({
      code: "anchor_locked",
      message: "The biweekly anchor can't change after biweekly runs exist.",
    });
  });

  // PAY-200, C14: field validation without values.
  it.each([
    [{ biweekly_anchor: "2026-01-06" }, "body.biweekly_anchor"],
    [{ biweekly_anchor: "2026-02-30" }, "body.biweekly_anchor"],
    [{ timezone: "" }, "body.timezone"],
    [{ timezone: `Mars/${"Olympus".repeat(10)}` }, "body.timezone"],
    [{ tax_rate_bps: 5001 }, "body.tax_rate_bps"],
    [{ retirement_bps: -1 }, "body.retirement_bps"],
    [{ tax_rate_bps: 12.5 }, "body.tax_rate_bps"],
    [{ name: "  " }, "body.name"],
    [{ extra: true }, "body"],
  ])("rejects %j", async (over, field) => {
    const cookie = await signIn();
    const res = await put(cookie, { ...BODY, ...over });
    expect(res.status).toBe(422);
    const e = await errorOf(res);
    expect((e.details as { field: string }[])[0]!.field).toBe(field);
    expect(JSON.stringify(e)).not.toContain("Mars");
    expect(seen).toHaveLength(0);
  });

  // PAY-200: the BFF checks the zone's format and length only; payroll's zoneinfo check is
  // authoritative, so zones missing from the runtime's ICU list (Etc/UTC, Asia/Kolkata) save.
  it.each(["Etc/UTC", "Asia/Kolkata", "Europe/Kyiv"])("forwards %s to payroll", async (zone) => {
    const cookie = await signIn();
    let sent: unknown;
    server.use(
      http.put(`${PAYROLL}/v1/company/settings`, async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json(settings());
      }),
    );
    expect((await put(cookie, { ...BODY, timezone: zone })).status).toBe(200);
    expect(sent).toMatchObject({ timezone: zone });
  });

  // PAY-200: UTC is accepted even where the runtime lists it only as an alias.
  it("accepts UTC", async () => {
    const cookie = await signIn();
    server.use(http.put(`${PAYROLL}/v1/company/settings`, () => HttpResponse.json(settings())));
    expect((await put(cookie, { ...BODY, timezone: "UTC" })).status).toBe(200);
  });

  // PAY-602: payroll's own 422 details are forwarded as [{field, issue}].
  it("forwards payroll validation details", async () => {
    const cookie = await signIn();
    server.use(
      http.put(`${PAYROLL}/v1/company/settings`, () =>
        HttpResponse.json(
          errorBody("validation_error", [
            { field: "body.timezone", issue: "timezone", input: "x" },
          ]),
          { status: 422 },
        ),
      ),
    );
    const res = await put(cookie, BODY);
    expect((await errorOf(res)).details).toEqual([{ field: "body.timezone", issue: "timezone" }]);
  });

  // PAY-604: Origin.
  it("is 403 bad_origin from another origin", async () => {
    const cookie = await signIn();
    const res = await putSettings(
      makeReq("/api/settings", { method: "PUT", cookie, body: BODY, origin: "https://evil" }),
      params(),
    );
    expect((await errorOf(res)).code).toBe("bad_origin");
  });

  // PAY-602: POST → 405 with Allow.
  it("is 405 for POST", async () => {
    const res = await postSettings(makeReq("/api/settings", { method: "POST" }), params());
    expect(res.status).toBe(405);
    expect(res.headers.get("allow")).toBe("GET, PUT");
  });
});
