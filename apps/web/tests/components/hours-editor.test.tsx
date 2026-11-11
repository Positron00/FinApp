// @vitest-environment jsdom
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { chunk, HoursEditor } from "@/components/runs/hours-editor";

import { payRun, payslip, RUN_ID } from "../helpers/fixtures";
import { apiError, renderWithClient, stubApi } from "../helpers/ui";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const slipsPath = `/api/pay-runs/${RUN_ID}/payslips`;
const hoursPath = `/api/pay-runs/${RUN_ID}/hours`;
const empId = (n: number) => `0193a000-0000-7000-8000-0000000001${String(n).padStart(2, "0")}`;

function hourly(n: number, hours: number | null = null) {
  return payslip({
    id: `0193a000-0000-7000-8000-0000000002${String(n).padStart(2, "0")}`,
    employee_id: empId(n),
    employee_name: `Hourly ${n}`,
    pay_type: "hourly",
    annual_salary_cents: null,
    hourly_rate_cents: 2500,
    hours_hundredths: hours,
    status: "pending",
  });
}

/** Three pages of payslips: page 1 and 2 by cursor, then the last (C8). */
function threePages(c: { path: string }) {
  if (!c.path.startsWith(slipsPath)) return undefined;
  const u = new URL(c.path, "http://x");
  expect(u.searchParams.get("limit")).toBe("100");
  const cursor = u.searchParams.get("cursor");
  if (cursor === null) return { body: { items: [hourly(1, 8000), payslip()], next_cursor: "p2" } };
  if (cursor === "p2") return { body: { items: [hourly(2)], next_cursor: "p3" } };
  return { body: { items: [hourly(3, 4000)], next_cursor: null } };
}

describe("HoursEditor", () => {
  // PAY-301, C8: loads every payslip page (following next_cursor) before showing the rows.
  it("follows next_cursor across 3 pages and lists only hourly rows", async () => {
    const calls = stubApi(threePages);
    renderWithClient(<HoursEditor run={payRun({ status: "draft" }) as never} />);
    expect(await screen.findByLabelText("Hours for Hourly 3")).toBeTruthy();
    expect(calls.filter((c) => c.path.startsWith(slipsPath))).toHaveLength(3);
    expect((screen.getByLabelText("Hours for Hourly 1") as HTMLInputElement).value).toBe("80.00");
    expect((screen.getByLabelText("Hours for Hourly 2") as HTMLInputElement).value).toBe("");
    expect(screen.queryByText("Ada Employee")).toBeNull(); // salaried rows aren't listed
  });

  // PAY-301, C7: saves only changed rows as hundredths, then the run is refetched.
  it("saves the changed rows and invalidates the run", async () => {
    const calls = stubApi((c) => {
      if (c.method === "PUT" && c.path === hoursPath)
        return { body: { pay_run_id: RUN_ID, status: "draft", entries: [] } };
      return threePages(c);
    });
    const { client } = renderWithClient(
      <HoursEditor run={payRun({ status: "calculated" }) as never} />,
    );
    const spy = vi.spyOn(client, "invalidateQueries");
    expect(await screen.findByText(/moves this run back to draft/)).toBeTruthy();
    await userEvent.type(screen.getByLabelText("Hours for Hourly 2"), "80.5");
    await userEvent.clear(screen.getByLabelText("Hours for Hourly 3"));
    await userEvent.type(screen.getByLabelText("Hours for Hourly 3"), "40");
    await userEvent.click(screen.getByRole("button", { name: "Save hours" }));
    expect(await screen.findByRole("status")).toBeTruthy();
    const put = calls.filter((c) => c.method === "PUT");
    expect(put).toHaveLength(1);
    // Hourly 3 typed back to its stored value: not sent
    expect(put[0]!.body).toEqual({ entries: [{ employee_id: empId(2), hours_hundredths: 8050 }] });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["pay-run", RUN_ID] });
  });

  // PAY-301, PAY-NFR-14: a bad value is refused locally, marked, and focused.
  it("refuses malformed hours without a request", async () => {
    const calls = stubApi(threePages);
    renderWithClient(<HoursEditor run={payRun({ status: "draft" }) as never} />);
    const input = await screen.findByLabelText("Hours for Hourly 2");
    await userEvent.type(input, "12.345");
    await userEvent.click(screen.getByRole("button", { name: "Save hours" }));
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(input);
    expect(screen.getByText(/Enter hours like 80/)).toBeTruthy();
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  // PAY-301, C15: 422 not_hourly / employee_not_in_run highlight the rows in details.employee_ids.
  it("highlights the rows a 422 names", async () => {
    stubApi((c) => {
      if (c.method === "PUT")
        return {
          status: 422,
          body: {
            error: {
              code: "employee_not_in_run",
              message: "1 employee(s) aren't in this run. Reload the run and try again.",
              request_id: "r1",
              details: { employee_ids: [empId(2)] },
            },
          },
        };
      return threePages(c);
    });
    renderWithClient(<HoursEditor run={payRun({ status: "draft" }) as never} />);
    await userEvent.type(await screen.findByLabelText("Hours for Hourly 2"), "10");
    await userEvent.click(screen.getByRole("button", { name: "Save hours" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/aren't in this run/);
    expect(screen.getByLabelText("Hours for Hourly 2").getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByLabelText("Hours for Hourly 1").getAttribute("aria-invalid")).toBeNull();
  });

  // PAY-302: calculate's hours_missing rows (passed in) are highlighted.
  it("highlights hours_missing rows from calculate", async () => {
    stubApi(threePages);
    renderWithClient(
      <HoursEditor run={payRun({ status: "draft" }) as never} highlight={[empId(2)]} />,
    );
    expect((await screen.findByLabelText("Hours for Hourly 2")).getAttribute("aria-invalid")).toBe(
      "true",
    );
  });

  // PAY-301: no hourly payslips → nothing to edit; a load error shows the banner.
  it("renders nothing without hourly rows, and a banner on error", async () => {
    stubApi((c) =>
      c.path.startsWith(slipsPath)
        ? { body: { items: [payslip()], next_cursor: null } }
        : undefined,
    );
    const { container } = renderWithClient(
      <HoursEditor run={payRun({ status: "draft" }) as never} />,
    );
    await waitFor(() => expect(container.textContent).toBe(""));
    cleanup();
    stubApi(() => apiError(404, "pay_run_not_found", "That pay run wasn't found."));
    renderWithClient(<HoursEditor run={payRun({ status: "draft" }) as never} />);
    expect((await screen.findByRole("alert")).textContent).toMatch(/wasn't found/);
  });

  // PAY-301: more than 500 changed rows go in sequential requests of 500.
  it("chunks entries by 500", () => {
    const items = Array.from({ length: 1203 }, (_, i) => i);
    expect(chunk(items, 500).map((c) => c.length)).toEqual([500, 500, 203]);
  });
});
