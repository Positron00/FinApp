import type { ReactElement } from "react";

import { RUN_ID } from "../helpers/fixtures";

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
});
