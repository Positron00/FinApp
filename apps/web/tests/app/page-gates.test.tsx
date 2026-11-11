import type { ReactElement } from "react";

import { EMPLOYEE_ID, RUN_ID } from "../helpers/fixtures";

/**
 * PAY-601: every server component under `(app)` gates itself with `requireSessionForPage`,
 * because the layout isn't re-rendered on client navigation and proxy.ts isn't a boundary.
 */
const gate = vi.hoisted(() => ({
  calls: [] as { path: string; roles?: string[] }[],
  result: "forbidden" as unknown,
}));
vi.mock("@/lib/require-session", () => ({
  resetRecheckForTests: () => undefined, // the shared setup resets server state
  requireSessionForPage: vi.fn(async (path: string, roles?: string[]) => {
    gate.calls.push({ path, roles });
    return gate.result;
  }),
}));
const nav = vi.hoisted(() => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND");
  }),
}));
vi.mock("next/navigation", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  notFound: nav.notFound,
}));
vi.mock("next/headers", () => ({
  headers: async () => new Headers({ "x-pathname": "/pay-runs?tab=x" }),
}));

const SESSION = { user_id: "u", name: "N", roles: ["payroll_admin", "approver"] };

type Page = (props: never) => Promise<ReactElement>;
const PAGES: [string, () => Promise<{ default: unknown }>, unknown, string, string[]?][] = [
  ["dashboard", () => import("@/app/(app)/dashboard/page"), {}, "/dashboard"],
  ["pay-runs", () => import("@/app/(app)/pay-runs/page"), {}, "/pay-runs"],
  [
    "pay-runs/new",
    () => import("@/app/(app)/pay-runs/new/page"),
    {},
    "/pay-runs/new",
    ["payroll_admin"],
  ],
  [
    "pay-runs/[id]",
    () => import("@/app/(app)/pay-runs/[id]/page"),
    { params: Promise.resolve({ id: RUN_ID }) },
    `/pay-runs/${RUN_ID}`,
  ],
  // F5 pages (PAY-109, PAY-200–203, PAY-101/102/108)
  ["employees", () => import("@/app/(app)/employees/page"), {}, "/employees"],
  [
    "employees/new",
    () => import("@/app/(app)/employees/new/page"),
    {},
    "/employees/new",
    ["payroll_admin"],
  ],
  [
    "employees/[id]",
    () => import("@/app/(app)/employees/[id]/page"),
    { params: Promise.resolve({ id: EMPLOYEE_ID }) },
    `/employees/${EMPLOYEE_ID}`,
  ],
  ["settings", () => import("@/app/(app)/settings/page"), {}, "/settings"],
  ["users", () => import("@/app/(app)/users/page"), {}, "/users", ["admin"]],
  ["audit", () => import("@/app/(app)/audit/page"), {}, "/audit", ["admin"]],
];

beforeEach(() => {
  gate.calls.length = 0;
});

describe("(app) page gates", () => {
  // PAY-601: each page calls requireSessionForPage with its path and roles.
  it.each(PAGES)("%s calls requireSessionForPage", async (_n, load, props, path, roles) => {
    gate.result = SESSION;
    const Page = (await load()).default as Page;
    await Page(props as never);
    expect(gate.calls).toEqual([{ path, roles }]);
  });

  // PAY-601: a "forbidden" answer renders <Forbidden/>, never the page content.
  it.each(PAGES)("%s renders Forbidden on 403", async (_n, load, props) => {
    gate.result = "forbidden";
    const { Forbidden } = await import("@/components/forbidden");
    const Page = (await load()).default as Page;
    const el = await Page(props as never);
    expect(el.type).toBe(Forbidden);
  });

  // PAY-601: the layout gates too, with the path proxy.ts passed in x-pathname.
  it("the (app) layout calls requireSessionForPage", async () => {
    gate.result = SESSION;
    const { default: Layout } = await import("@/app/(app)/layout");
    await Layout({ children: null });
    expect(gate.calls).toEqual([{ path: "/pay-runs?tab=x", roles: undefined }]);
  });

  // PAY-601: a malformed [id] is a 404 before any session work.
  it.each([
    ["pay-runs/[id]", () => import("@/app/(app)/pay-runs/[id]/page")],
    ["employees/[id]", () => import("@/app/(app)/employees/[id]/page")],
  ])("%s with a malformed id is notFound()", async (_n, load) => {
    const Page = (await load()).default as Page;
    await expect(Page({ params: Promise.resolve({ id: "not-a-uuid" }) } as never)).rejects.toThrow(
      "NEXT_NOT_FOUND",
    );
    expect(gate.calls).toEqual([]);
  });
});
