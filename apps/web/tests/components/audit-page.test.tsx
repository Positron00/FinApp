// @vitest-environment jsdom
import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AuditPage, detailKeys } from "@/components/audit/audit-page";

import { authAuditEvent, payrollAuditEvent } from "../helpers/fixtures";
import { renderWithClient, stubApi } from "../helpers/ui";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const SECRET_VALUE = "value-must-not-render";

describe("AuditPage", () => {
  // PAY-109, plan §8, PAY-NFR-14: two WAI-ARIA tabs; details show key names only; own pagers.
  it("shows both logs with key names only, each paged on its own", async () => {
    const calls = stubApi((c) => {
      if (c.path.startsWith("/api/audit/auth")) {
        return c.path.includes("cursor=a2")
          ? { body: { items: [authAuditEvent({ event: "auth.login_failed" })], next_cursor: null } }
          : {
              body: {
                items: [authAuditEvent({ details: { reason: SECRET_VALUE, ip_note: 1 } })],
                next_cursor: "a2",
              },
            };
      }
      if (c.path.startsWith("/api/audit/payroll")) {
        return {
          body: {
            items: [payrollAuditEvent({ details: { account_last4: SECRET_VALUE } })],
            next_cursor: null,
          },
        };
      }
      return undefined;
    });
    renderWithClient(<AuditPage />);
    await screen.findByText("user.invited");
    expect(screen.getByText("reason, ip_note")).toBeTruthy();
    expect(screen.getByText("abcd1234")).toBeTruthy(); // ip_hash truncated
    expect(document.body.textContent).not.toContain(SECRET_VALUE);

    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("auth.login_failed");
    expect(calls.at(-1)!.path).toBe("/api/audit/auth?limit=50&cursor=a2");

    // keyboard: ArrowRight moves to the payroll tab, focus follows
    const [authTab, payrollTab] = screen.getAllByRole("tab");
    authTab!.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(payrollTab);
    expect(payrollTab!.getAttribute("aria-selected")).toBe("true");
    await screen.findByText("employee.updated");
    expect(screen.getByText("account_last4")).toBeTruthy();
    expect(document.body.textContent).not.toContain(SECRET_VALUE);
    expect(calls.some((c) => c.path === "/api/audit/payroll?limit=50")).toBe(true);
    // the inactive panel is hidden and unmounted
    expect(screen.queryByText("auth.login_failed")).toBeNull();
  });

  // PAY-601, PAY-602: a load failure shows the mapped message in the error banner.
  it("shows an error when a log can't load", async () => {
    stubApi(() => ({
      status: 403,
      body: {
        error: {
          code: "forbidden_role",
          message: "You don't have permission to do that.",
          request_id: "r9",
        },
      },
    }));
    renderWithClient(<AuditPage />);
    expect((await screen.findByRole("alert")).textContent).toContain("permission");
  });

  // PAY-NFR-07: detailKeys never returns values.
  it("lists keys only", () => {
    expect(detailKeys({})).toBe("—");
    expect(detailKeys({ a: "secret", b: 2 })).toBe("a, b");
  });
});
