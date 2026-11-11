// @vitest-environment jsdom
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { SettingsView } from "@/components/forms/settings-form";

import { settings } from "../helpers/fixtures";
import { USER_ID } from "../helpers/request";
import { apiError, renderWithClient, stubApi, type Call } from "../helpers/ui";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ADMIN = { id: USER_ID, isAdmin: true };
const puts = (calls: Call[]) => calls.filter((c) => c.method === "PUT");

async function setText(label: RegExp, value: string) {
  const el = screen.getByLabelText(label);
  await userEvent.clear(el);
  if (value) await userEvent.type(el, value);
}

describe("SettingsView", () => {
  // PAY-200, PAY-601: non-admins see the settings read-only; updated_by shows "you" (§6.1).
  it("is read-only for non-admins", async () => {
    stubApi(() => ({ body: settings() }));
    renderWithClient(<SettingsView me={{ id: USER_ID, isAdmin: false }} />);
    await screen.findByText("Acme Ltd");
    expect(screen.getByText("20.00%")).toBeTruthy();
    expect(screen.getByText("Required")).toBeTruthy();
    expect(screen.getByText(/by you\./)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save settings" })).toBeNull();
  });

  // PAY-200, C15: company_settings_not_found replaces the form with its message.
  it("shows company_settings_not_found instead of the form", async () => {
    stubApi(() =>
      apiError(
        404,
        "company_settings_not_found",
        "Company settings haven't been set up yet. Ask an admin.",
      ),
    );
    renderWithClient(<SettingsView me={ADMIN} />);
    expect((await screen.findByRole("alert")).textContent).toContain("haven't been set up");
    expect(screen.queryByRole("button", { name: "Save settings" })).toBeNull();
  });

  // PAY-200, ADR 0006: percent text → basis points with integer arithmetic.
  it("PUTs the settings with rates in basis points", async () => {
    const calls = stubApi((c) =>
      c.method === "PUT"
        ? { body: settings({ tax_rate_bps: 765, retirement_bps: 250, updated_by: "x" }) }
        : { body: settings() },
    );
    renderWithClient(<SettingsView me={ADMIN} />);
    await screen.findByRole("button", { name: "Save settings" });
    await setText(/^Tax rate/, "7.65");
    await setText(/^Retirement rate/, "2.5");
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Settings saved.");
    expect(puts(calls)[0]!.body).toEqual({
      name: "Acme Ltd",
      tax_rate_bps: 765,
      retirement_bps: 250,
      timezone: "America/New_York",
      biweekly_anchor: "2026-01-05",
      require_dual_approval: true,
    });
  });

  // PAY-200: "20" → 2000 bps; a rate above the bound is refused before sending.
  it("checks rate bounds and formats client-side", async () => {
    const calls = stubApi(() => ({ body: settings() }));
    renderWithClient(<SettingsView me={ADMIN} />);
    await screen.findByRole("button", { name: "Save settings" });
    await setText(/^Tax rate/, "60");
    await setText(/^Retirement rate/, "abc");
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("The tax rate must be between 0% and 50%.");
    expect(screen.getByText(/Enter a percentage like 5/)).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText(/^Tax rate/)));
    expect(puts(calls)).toHaveLength(0);
  });

  // PAY-200: the biweekly anchor must be a Monday (client feedback).
  it("refuses a non-Monday anchor", async () => {
    const calls = stubApi(() => ({ body: settings() }));
    renderWithClient(<SettingsView me={ADMIN} />);
    await screen.findByRole("button", { name: "Save settings" });
    await setText(/^Biweekly anchor/, "2026-01-06");
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Choose a Monday.");
    expect(puts(calls)).toHaveLength(0);
  });

  // PAY-200, R33: 409 anchor_locked shows on the anchor field.
  it("shows anchor_locked on the anchor field", async () => {
    stubApi((c) =>
      c.method === "PUT"
        ? apiError(
            409,
            "anchor_locked",
            "The biweekly anchor can't change after biweekly runs exist.",
          )
        : { body: settings() },
    );
    renderWithClient(<SettingsView me={ADMIN} />);
    await screen.findByRole("button", { name: "Save settings" });
    await setText(/^Biweekly anchor/, "2026-01-12");
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText(/can't change after biweekly runs exist/);
    expect(screen.getByLabelText(/^Biweekly anchor/).getAttribute("aria-invalid")).toBe("true");
    // PAY-NFR-14: focus is on the field as soon as the error renders.
    expect(document.activeElement).toBe(screen.getByLabelText(/^Biweekly anchor/));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  // PAY-602, C56: 422 details show inline on the named field; the summary is in the alert.
  it("shows 422 details inline", async () => {
    stubApi((c) =>
      c.method === "PUT"
        ? {
            status: 422,
            body: {
              error: {
                code: "validation_error",
                message: "Some fields need attention.",
                request_id: "r2",
                details: [{ field: "body.timezone", issue: "timezone" }],
              },
            },
          }
        : { body: settings() },
    );
    renderWithClient(<SettingsView me={ADMIN} />);
    await userEvent.click(await screen.findByRole("button", { name: "Save settings" }));
    await screen.findByText("Choose a time zone from the list.");
    expect(screen.getByRole("alert").textContent).toContain("Reference: r2");
  });

  // PAY-200, ADR 0005: turning dual approval off asks first; Cancel keeps it on.
  it("confirms before turning dual approval off", async () => {
    const calls = stubApi((c) =>
      c.method === "PUT"
        ? { body: settings({ require_dual_approval: false }) }
        : { body: settings() },
    );
    renderWithClient(<SettingsView me={ADMIN} />);
    const toggle = (await screen.findByRole("switch")) as HTMLInputElement;
    await userEvent.click(toggle);
    let dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByText("Runs already calculated keep dual approval.")).toBeTruthy();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(toggle.checked).toBe(true);
    await userEvent.click(toggle);
    dialog = screen.getByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Turn off" }));
    expect(toggle.checked).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Settings saved.");
    expect(puts(calls)[0]!.body).toMatchObject({ require_dual_approval: false });
  });
});
