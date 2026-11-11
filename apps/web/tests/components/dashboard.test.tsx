// @vitest-environment jsdom
import { cleanup, screen } from "@testing-library/react";

import { Dashboard, employeeCount } from "@/components/dashboard";

import { employee, payRun } from "../helpers/fixtures";
import { renderWithClient, stubApi } from "../helpers/ui";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const P1 = "0193a000-0000-7000-8000-0000000000b2";
const F1 = "0193a000-0000-7000-8000-0000000000b3";

describe("Dashboard", () => {
  // PAY-303, PAY-413: the four reads, the blocked count and "needs attention"; quick actions.
  it("shows the cards from the four reads", async () => {
    const calls = stubApi((c) => {
      if (c.path === "/api/pay-runs?limit=10&status=processing") {
        return {
          body: {
            items: [
              payRun({
                id: P1,
                status: "processing",
                period_start: "2026-10-01",
                period_end: "2026-10-31",
                counts: {
                  requested: 3,
                  awaiting_bank: 0,
                  blocked: 2,
                  paid: 1,
                  no_transfer: 0,
                  failed: 0,
                },
              }),
            ],
            next_cursor: null,
          },
        };
      }
      if (c.path === "/api/pay-runs?limit=10&status=partially_failed") {
        return {
          body: {
            items: [payRun({ id: F1, status: "partially_failed", period_start: "2026-09-01" })],
            next_cursor: null,
          },
        };
      }
      if (c.path === "/api/pay-runs?limit=10") return { body: { items: [], next_cursor: null } };
      if (c.path === "/api/employees?status=active&limit=50") {
        return { body: { items: [employee(), employee()], next_cursor: "more" } };
      }
      return undefined;
    });
    renderWithClient(<Dashboard canPrepare />);
    await screen.findByText("2 blocked");
    expect(await screen.findByText("needs attention")).toBeTruthy();
    expect(await screen.findByText("2+")).toBeTruthy();
    expect(screen.getByText("None.")).toBeTruthy(); // latest runs empty
    expect(screen.getByRole("link", { name: "New pay run" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Add employee" })).toBeTruthy();
    expect(new Set(calls.map((c) => c.path)).size).toBe(4);
  });

  // PAY-601: no quick actions without payroll_admin.
  it("hides quick actions without canPrepare", async () => {
    stubApi(() => ({ body: { items: [], next_cursor: null } }));
    renderWithClient(<Dashboard canPrepare={false} />);
    await screen.findByText("0");
    expect(screen.queryByRole("link", { name: "New pay run" })).toBeNull();
  });

  // PAY-202 (§6.2): exact count on the last page, "N+" otherwise.
  it("counts employees", () => {
    expect(employeeCount({ items: [employee() as never], next_cursor: null })).toBe("1");
    expect(employeeCount({ items: [employee() as never], next_cursor: "x" })).toBe("1+");
  });
});
