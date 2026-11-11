// @vitest-environment jsdom
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { bankErrors } from "@/components/employees/bank-fields";
import { EmployeeDetail } from "@/components/employees/employee-detail";
import { bankLabel, EmployeeList, payBasisLabel } from "@/components/employees/employee-list";
import { EmployeeForm } from "@/components/forms/employee-form";

import { employee, EMPLOYEE_ID, settings } from "../helpers/fixtures";
import { USER_ID } from "../helpers/request";
import { apiError, renderWithClient, stubApi, type Call } from "../helpers/ui";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

beforeEach(() => push.mockReset());
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const VALID_ROUTING = "021000021";
const empPath = `/api/employees/${EMPLOYEE_ID}`;

async function fillCommon(o: { routing?: string; account?: string } = {}) {
  await userEvent.type(screen.getByLabelText(/^Name/), "Ada Employee");
  await userEvent.type(screen.getByLabelText(/^Email/), "ada@payrun.example.com");
  await userEvent.type(screen.getByLabelText(/^Routing number/), o.routing ?? VALID_ROUTING);
  await userEvent.type(screen.getByLabelText(/^Account number/), o.account ?? "123456789");
}

function posts(calls: Call[]) {
  return calls.filter((c) => c.method !== "GET");
}

describe("EmployeeForm (create)", () => {
  // PAY-201, ADR 0006: a salaried employee; money text → integer cents; push to the new page.
  it("creates a salaried employee", async () => {
    const calls = stubApi((c) =>
      c.method === "POST"
        ? { status: 201, body: employee() }
        : c.path === "/api/settings"
          ? { body: settings() }
          : undefined,
    );
    renderWithClient(<EmployeeForm />);
    await fillCommon();
    await userEvent.type(screen.getByLabelText(/^Annual salary \(USD\)/), "52000.5");
    await userEvent.click(screen.getByRole("button", { name: "Add employee" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/employees/${EMPLOYEE_ID}`));
    expect(posts(calls)[0]!.body).toEqual({
      name: "Ada Employee",
      email: "ada@payrun.example.com",
      schedule: "monthly",
      pay_type: "salaried",
      annual_salary_cents: 5_200_050,
      bank_routing: VALID_ROUTING,
      bank_account: "123456789",
    });
  });

  // PAY-201: the pay-type switch shows the rate field; hourly body; biweekly anchor hint.
  it("creates an hourly biweekly employee", async () => {
    const calls = stubApi((c) =>
      c.method === "POST"
        ? { status: 201, body: employee({ pay_type: "hourly" }) }
        : { body: settings() },
    );
    renderWithClient(<EmployeeForm />);
    expect(screen.queryByLabelText(/^Hourly rate \(USD\)/)).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Pay type"), "hourly");
    expect(screen.queryByLabelText(/^Annual salary \(USD\)/)).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Pay schedule"), "biweekly");
    await screen.findByText(/every 14 days from 2026-01-05/);
    await fillCommon();
    await userEvent.type(screen.getByLabelText(/^Hourly rate \(USD\)/), "25.05");
    await userEvent.click(screen.getByRole("button", { name: "Add employee" }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(posts(calls)[0]!.body).toMatchObject({
      pay_type: "hourly",
      schedule: "biweekly",
      hourly_rate_cents: 2505,
    });
    expect(posts(calls)[0]!.body).not.toHaveProperty("annual_salary_cents");
  });

  // PAY-201, PAY-NFR-14: the ABA checksum fails inline; focus moves to the first invalid field.
  it("checks the routing checksum and the account length before sending", async () => {
    const calls = stubApi(() => ({ body: settings() }));
    renderWithClient(<EmployeeForm />);
    await fillCommon({ routing: "123456789", account: "123" });
    await userEvent.type(screen.getByLabelText(/^Annual salary \(USD\)/), "52000");
    expect(screen.getAllByText(/routing number isn't valid/).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Add employee" }));
    await screen.findByText("Enter 4 to 17 digits.");
    const routing = screen.getByLabelText(/^Routing number/);
    await waitFor(() => expect(document.activeElement).toBe(routing));
    expect(routing.getAttribute("aria-invalid")).toBe("true");
    expect(posts(calls)).toHaveLength(0);
  });

  // ADR 0006: a malformed amount is a field error, never a float.
  it("rejects a malformed salary", async () => {
    const calls = stubApi(() => ({ body: settings() }));
    renderWithClient(<EmployeeForm />);
    await fillCommon();
    await userEvent.type(screen.getByLabelText(/^Annual salary \(USD\)/), "52,000.123");
    await userEvent.click(screen.getByRole("button", { name: "Add employee" }));
    await screen.findByText(/Enter an amount/);
    expect(posts(calls)).toHaveLength(0);
  });

  // PAY-201: 409 employee_email_taken shows on the email field.
  it("shows employee_email_taken on the email field", async () => {
    stubApi((c) =>
      c.method === "POST"
        ? apiError(409, "employee_email_taken", "An employee with this email already exists.")
        : { body: settings() },
    );
    renderWithClient(<EmployeeForm />);
    await fillCommon();
    await userEvent.type(screen.getByLabelText(/^Annual salary \(USD\)/), "52000");
    await userEvent.click(screen.getByRole("button", { name: "Add employee" }));
    await screen.findByText("An employee with this email already exists.");
    const email = screen.getByLabelText(/^Email/);
    expect(email.getAttribute("aria-invalid")).toBe("true");
    await waitFor(() => expect(document.activeElement).toBe(email));
    expect(screen.queryByRole("alert")).toBeNull(); // on the field, not in the banner
  });

  // PAY-602, C14, C56: 422 details (body.<field>) show inline on that field.
  it("shows 422 details inline", async () => {
    stubApi((c) =>
      c.method === "POST"
        ? {
            status: 422,
            body: {
              error: {
                code: "validation_error",
                message: "Some fields need attention.",
                request_id: "r1",
                details: [{ field: "body.bank_routing", issue: "aba_checksum" }],
              },
            },
          }
        : { body: settings() },
    );
    renderWithClient(<EmployeeForm />);
    await fillCommon();
    await userEvent.type(screen.getByLabelText(/^Annual salary \(USD\)/), "52000");
    await userEvent.click(screen.getByRole("button", { name: "Add employee" }));
    await screen.findByText("Some fields need attention.");
    expect(screen.getByLabelText(/^Routing number/).getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByText(/routing number isn't valid/)).toBeTruthy();
  });
});

describe("EmployeeForm (edit)", () => {
  // PAY-203: only the changed fields are sent; bank fields aren't part of the edit form.
  it("PATCHes only what changed", async () => {
    const calls = stubApi((c) =>
      c.method === "PATCH" ? { body: employee({ name: "Ada Lovelace" }) } : undefined,
    );
    renderWithClient(<EmployeeForm employee={employee() as never} />);
    expect(screen.queryByLabelText(/^Routing number/)).toBeNull();
    const name = screen.getByLabelText(/^Name/);
    await userEvent.clear(name);
    await userEvent.type(name, "Ada Lovelace");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved.");
    expect(calls).toHaveLength(1);
    expect(calls[0]!.path).toBe(empPath);
    expect(calls[0]!.body).toEqual({ name: "Ada Lovelace" });
  });

  // PAY-203: switching the pay type sends the new basis with it.
  it("sends the new basis on a pay-type switch", async () => {
    const calls = stubApi(() => ({ body: employee({ pay_type: "hourly" }) }));
    renderWithClient(<EmployeeForm employee={employee() as never} />);
    await userEvent.selectOptions(screen.getByLabelText("Pay type"), "hourly");
    await userEvent.type(screen.getByLabelText(/^Hourly rate \(USD\)/), "30");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved.");
    expect(calls[0]!.body).toEqual({ pay_type: "hourly", hourly_rate_cents: 3000 });
  });

  // PAY-203: no change → no request.
  it("sends nothing when nothing changed", async () => {
    const calls = stubApi(() => undefined);
    renderWithClient(<EmployeeForm employee={employee() as never} />);
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved.");
    expect(calls).toHaveLength(0);
  });
});

describe("EmployeeDetail", () => {
  const withBank = employee({ account_last4: "6789", has_bank_details: true });

  // PAY-202, PAY-NFR-08: only ••••last4; who changed bank details; edit controls for payroll_admin.
  it("shows the last 4 digits only and the edit controls", async () => {
    stubApi((c) => (c.path === empPath ? { body: withBank } : undefined));
    renderWithClient(
      <EmployeeDetail employeeId={EMPLOYEE_ID} me={{ id: USER_ID, canEdit: true }} />,
    );
    await screen.findByText("••••6789");
    expect(screen.getByText(/by you/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Change bank details" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Deactivate" })).toBeTruthy();
  });

  // PAY-601: no edit controls without payroll_admin.
  it("hides the edit controls for other roles", async () => {
    stubApi((c) => (c.path === empPath ? { body: withBank } : undefined));
    renderWithClient(
      <EmployeeDetail employeeId={EMPLOYEE_ID} me={{ id: USER_ID, canEdit: false }} />,
    );
    await screen.findByText("••••6789");
    expect(screen.queryByRole("button", { name: "Change bank details" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deactivate" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
  });

  // PAY-203, ADR 0004: the bank dialog is empty (never prefilled) and PATCHes both fields.
  it("changes bank details in a dialog", async () => {
    const calls = stubApi((c) =>
      c.method === "PATCH"
        ? { body: employee({ account_last4: "4321", has_bank_details: true }) }
        : { body: withBank },
    );
    renderWithClient(
      <EmployeeDetail employeeId={EMPLOYEE_ID} me={{ id: USER_ID, canEdit: true }} />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Change bank details" }));
    const dialog = screen.getByRole("dialog");
    const routing = within(dialog).getByLabelText(/^Routing number/) as HTMLInputElement;
    const account = within(dialog).getByLabelText(/^Account number/) as HTMLInputElement;
    expect(routing.value).toBe("");
    expect(account.value).toBe("");
    await userEvent.type(routing, VALID_ROUTING);
    await userEvent.type(account, "987654321");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save bank details" }));
    await screen.findByText("Bank details changed.");
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({
      bank_routing: VALID_ROUTING,
      bank_account: "987654321",
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.textContent).not.toContain("987654321");
  });

  // PAY-203: deactivate is a confirm that PATCHes {status: "inactive"}.
  it("deactivates after a confirm", async () => {
    const calls = stubApi((c) =>
      c.method === "PATCH"
        ? { body: employee({ status: "inactive", deactivated_at: "2026-11-11T00:00:00Z" }) }
        : { body: withBank },
    );
    renderWithClient(
      <EmployeeDetail employeeId={EMPLOYEE_ID} me={{ id: USER_ID, canEdit: true }} />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Deactivate" }));
    const dialog = screen.getByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Deactivate" }));
    await screen.findByText("Employee deactivated.");
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ status: "inactive" });
    expect(screen.getByRole("button", { name: "Reactivate" })).toBeTruthy();
  });

  // PAY-203, PAY-608: reactivating after a purge needs new bank details in the same PATCH.
  it("requires new bank details to reactivate after a purge", async () => {
    const purged = employee({ status: "inactive", has_bank_details: false, account_last4: null });
    const calls = stubApi((c) =>
      c.method === "PATCH"
        ? { body: employee({ account_last4: "4321", has_bank_details: true }) }
        : { body: purged },
    );
    renderWithClient(
      <EmployeeDetail employeeId={EMPLOYEE_ID} me={{ id: USER_ID, canEdit: true }} />,
    );
    expect((await screen.findAllByText("No bank details")).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Reactivate" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Reactivate" }));
    expect(within(dialog).getByText("Enter the 9-digit routing number.")).toBeTruthy();
    expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(0);
    await userEvent.type(within(dialog).getByLabelText(/^Routing number/), VALID_ROUTING);
    await userEvent.type(within(dialog).getByLabelText(/^Account number/), "4321");
    await userEvent.click(within(dialog).getByRole("button", { name: "Reactivate" }));
    await screen.findByText("Employee reactivated.");
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({
      status: "active",
      bank_routing: VALID_ROUTING,
      bank_account: "4321",
    });
  });

  // PAY-203: with bank details on file, reactivation is a plain confirm.
  it("reactivates with a plain confirm when bank details exist", async () => {
    const calls = stubApi((c) =>
      c.method === "PATCH"
        ? { body: withBank }
        : { body: employee({ status: "inactive", account_last4: "6789", has_bank_details: true }) },
    );
    renderWithClient(
      <EmployeeDetail employeeId={EMPLOYEE_ID} me={{ id: USER_ID, canEdit: true }} />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Reactivate" }));
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).queryByLabelText(/^Routing number/)).toBeNull();
    await userEvent.click(within(dialog).getByRole("button", { name: "Reactivate" }));
    await screen.findByText("Employee reactivated.");
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ status: "active" });
  });

  // PAY-602: a PATCH error shows in the dialog's alert with its reference.
  it("shows a PATCH error in the dialog", async () => {
    stubApi((c) =>
      c.method === "PATCH"
        ? apiError(404, "employee_not_found", "That employee wasn't found.")
        : { body: withBank },
    );
    renderWithClient(
      <EmployeeDetail employeeId={EMPLOYEE_ID} me={{ id: USER_ID, canEdit: true }} />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Deactivate" }));
    const dialog = screen.getByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Deactivate" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain(
      "That employee wasn't found.",
    );
  });

  // PAY-202: a load failure shows the error banner.
  it("shows an error when the employee can't load", async () => {
    stubApi(() => apiError(404, "employee_not_found", "That employee wasn't found."));
    renderWithClient(
      <EmployeeDetail employeeId={EMPLOYEE_ID} me={{ id: USER_ID, canEdit: true }} />,
    );
    expect((await screen.findByRole("alert")).textContent).toContain("wasn't found");
  });
});

describe("EmployeeList", () => {
  // PAY-202, C8: columns with last 4 only; status filter resets the pager and is passed through.
  it("lists, filters and pages", async () => {
    const calls = stubApi((c) =>
      c.path.includes("cursor=c2")
        ? { body: { items: [employee({ name: "Bo Second" })], next_cursor: null } }
        : {
            body: {
              items: [employee({ account_last4: "6789", has_bank_details: true })],
              next_cursor: "c2",
            },
          },
    );
    renderWithClient(<EmployeeList canCreate />);
    await screen.findByText("••••6789");
    expect(screen.getByRole("link", { name: "Add employee" })).toBeTruthy();
    expect(screen.getByText("$60,000.00 / year")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Bo Second");
    expect(calls.at(-1)!.path).toBe("/api/employees?limit=50&cursor=c2");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "inactive");
    await waitFor(() => expect(calls.at(-1)!.path).toBe("/api/employees?limit=50&status=inactive"));
  });

  // PAY-601: no "Add employee" without payroll_admin; empty state.
  it("hides Add employee without canCreate", async () => {
    stubApi(() => ({ body: { items: [], next_cursor: null } }));
    renderWithClient(<EmployeeList canCreate={false} />);
    await screen.findByText("No employees.");
    expect(screen.queryByRole("link", { name: "Add employee" })).toBeNull();
  });

  // PAY-202, PAY-608: label helpers.
  it("formats the bank and pay basis labels", () => {
    expect(bankLabel({ account_last4: null, has_bank_details: false })).toBe("No bank details");
    expect(bankLabel({ account_last4: "0000", has_bank_details: true })).toBe("••••0000");
    expect(
      payBasisLabel({ pay_type: "hourly", annual_salary_cents: null, hourly_rate_cents: 2500 }),
    ).toBe("$25.00 / hour");
    expect(bankErrors({ bank_routing: VALID_ROUTING, bank_account: "1234" })).toEqual({});
    expect(bankErrors({ bank_routing: "12", bank_account: "1" })).toEqual({
      bank_routing: "Enter the 9-digit routing number.",
      bank_account: "Enter 4 to 17 digits.",
    });
  });
});
