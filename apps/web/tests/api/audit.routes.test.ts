import { http, HttpResponse } from "msw";

import { GET as authAudit, POST as authAuditPost } from "@/app/api/audit/auth/route";
import { GET as payrollAudit } from "@/app/api/audit/payroll/route";

import { IDENTITY, PAYROLL } from "../helpers/env";
import { authAuditEvent, payrollAuditEvent } from "../helpers/fixtures";
import { seen, server, setupMsw } from "../helpers/msw";
import { makeReq, params, signIn } from "../helpers/request";

setupMsw();

const CASES = [
  ["/api/audit/auth", authAudit, `${IDENTITY}/v1/audit-log`, authAuditEvent()],
  ["/api/audit/payroll", payrollAudit, `${PAYROLL}/v1/audit-events`, payrollAuditEvent()],
] as const;

describe.each(CASES)("GET %s", (path, handler, upstream, item) => {
  // PAY-109, plan §8, C8: admin; cursor and limit pass through; Page<T> returned.
  it("returns a page with pass-through", async () => {
    const cookie = await signIn({ roles: ["admin"] });
    server.use(http.get(upstream, () => HttpResponse.json({ items: [item], next_cursor: "nx" })));
    const res = await handler(makeReq(`${path}?cursor=cur1&limit=50`, { cookie }), params());
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ items: [item], next_cursor: "nx" });
    expect(Object.fromEntries(new URL(seen[0]!.url).searchParams)).toEqual({
      cursor: "cur1",
      limit: "50",
    });
  });

  // PAY-109, PAY-601: payroll_admin and approver get 403, with no upstream call.
  it("is 403 for non-admins", async () => {
    const cookie = await signIn({ roles: ["payroll_admin", "approver"] });
    const res = await handler(makeReq(path, { cookie }), params());
    expect(res.status).toBe(403);
    expect(seen).toHaveLength(0);
  });

  // PAY-106: 401 without a session; PAY-602: a bad limit → 422.
  it("requires a session and validates the query", async () => {
    expect((await handler(makeReq(path), params())).status).toBe(401);
    const cookie = await signIn();
    expect((await handler(makeReq(`${path}?limit=0`, { cookie }), params())).status).toBe(422);
  });

  // PAY-602: an invalid response (bad entity_type / non-UUID id) → 500.
  it("refuses an invalid response", async () => {
    const cookie = await signIn();
    server.use(
      http.get(upstream, () =>
        HttpResponse.json({ items: [{ ...item, id: "nope" }], next_cursor: null }),
      ),
    );
    expect((await handler(makeReq(path, { cookie }), params())).status).toBe(500);
  });
});

// PAY-602: other methods → 405.
it("audit routes are GET only", async () => {
  const res = await authAuditPost(makeReq("/api/audit/auth", { method: "POST" }), params());
  expect(res.status).toBe(405);
});
