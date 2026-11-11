// @vitest-environment jsdom
import { act, cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { ResolveDialog } from "@/components/runs/resolve-dialog";

import { attempt, payout, PAYOUT_ID, RUN_ID } from "../helpers/fixtures";
import { apiError, renderWithClient, stubApi } from "../helpers/ui";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const resolvePath = `/api/pay-runs/${RUN_ID}/payouts/${PAYOUT_ID}/resolve`;
const blocked = payout({
  status: "requested",
  attempts: [attempt({ status: "needs_operator", final_at: null, account_last4: "4242" })],
});

/** The dialog opened from a trigger button, as on the Payouts tab. */
function Harness({ onResolved }: { onResolved: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Resolve
      </button>
      {open ? (
        <ResolveDialog
          payout={blocked as never}
          onClose={() => setOpen(false)}
          onResolved={(m) => {
            setOpen(false);
            onResolved(m);
          }}
        />
      ) : null}
    </>
  );
}

async function open() {
  const onResolved = vi.fn();
  const r = renderWithClient(<Harness onResolved={onResolved} />);
  await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
  return { ...r, onResolved };
}

const submit = () =>
  screen
    .getAllByRole("button", { name: "Resolve" })
    .find((b) => b.closest("[role=alertdialog]")) as HTMLButtonElement;

describe("ResolveDialog", () => {
  // PAY-502, R8, PAY-NFR-14: the bank-check warning comes before the choices and gets the focus.
  it("shows the warning first, focused, with the payout details", async () => {
    stubApi(() => undefined);
    await open();
    const dialog = screen.getByRole("alertdialog", { name: "Resolve blocked payout" });
    const warning = screen.getByRole("heading", { name: "Check the bank first" });
    expect(document.activeElement).toBe(warning);
    const radios = screen.getAllByRole("radio");
    // DOCUMENT_POSITION_FOLLOWING: the radios come after the warning
    expect(warning.compareDocumentPosition(radios[0]!) & 4).toBe(4);
    expect(dialog.textContent).toContain("••••4242");
    expect(dialog.textContent).toContain("$3,750.00");
    expect(dialog.textContent).toContain("GET /v1/transfers/{key}");
    // PAY-NFR-14: Shift+Tab from the (non-tabbable) warning stays in the dialog
    await userEvent.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(screen.getAllByRole("button", { name: "Resolve" })[0]);
  });

  // PAY-502: no default choice; submit disabled until a choice and a 1–500 character note.
  it("requires a choice and a note", async () => {
    stubApi(() => undefined);
    await open();
    expect(screen.getAllByRole("radio").every((r) => !(r as HTMLInputElement).checked)).toBe(true);
    expect(submit().disabled).toBe(true);
    await userEvent.click(screen.getByRole("radio", { name: /Send again/ }));
    expect(submit().disabled).toBe(true);
    await userEvent.type(screen.getByLabelText(/Note for the audit log/), "   ");
    expect(submit().disabled).toBe(true); // whitespace only
    await userEvent.type(screen.getByLabelText(/Note for the audit log/), "bank has no record");
    expect(submit().disabled).toBe(false);
    // the counter is announced politely
    expect(screen.getByText(/of 500 characters/).getAttribute("aria-live")).toBe("polite");
  });

  // PAY-502: sends ResolveBody (note trimmed); success closes, notifies and invalidates the run.
  it("redrives and closes", async () => {
    const calls = stubApi((c) => (c.method === "POST" ? { body: blocked } : undefined));
    const { onResolved, client } = await open();
    const spy = vi.spyOn(client, "invalidateQueries");
    await userEvent.click(screen.getByRole("radio", { name: /Send again/ }));
    await userEvent.type(
      screen.getByLabelText(/Note for the audit log/),
      " checked bank: pending ",
    );
    await userEvent.click(submit());
    await waitFor(() => expect(onResolved).toHaveBeenCalledWith("Payout resent"));
    expect(calls[0]).toMatchObject({
      method: "POST",
      path: resolvePath,
      body: { action: "redrive", note: "checked bank: pending" },
    });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["pay-run", RUN_ID] });
    // PAY-NFR-14: focus returns to the trigger
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Resolve" }));
  });

  // PAY-502: mark_rejected success message.
  it("marks rejected", async () => {
    stubApi((c) => (c.method === "POST" ? { body: blocked } : undefined));
    const { onResolved } = await open();
    await userEvent.click(screen.getByRole("radio", { name: /Mark rejected/ }));
    await userEvent.type(screen.getByLabelText(/Note for the audit log/), "bank: no transfer");
    await userEvent.click(submit());
    await waitFor(() => expect(onResolved).toHaveBeenCalledWith("Payout marked rejected"));
  });

  // PAY-502, C48: resolve_too_early counts down from Retry-After; Mark rejected is disabled
  // until 0, while Send again stays available.
  it("counts down after resolve_too_early", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubApi((c) =>
      c.method === "POST"
        ? {
            ...apiError(409, "resolve_too_early", "This payout was blocked moments ago."),
            headers: { "retry-after": "3" },
          }
        : undefined,
    );
    await open();
    await userEvent.click(screen.getByRole("radio", { name: /Mark rejected/ }));
    await userEvent.type(screen.getByLabelText(/Note for the audit log/), "bank: nothing");
    await userEvent.click(submit());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Wait 3 seconds/);
    const reject = screen.getByRole("radio", { name: /Mark rejected/ }) as HTMLInputElement;
    expect(reject.disabled).toBe(true);
    expect(screen.getByRole("alertdialog")).toBeTruthy(); // still open
    expect((screen.getByRole("radio", { name: /Send again/ }) as HTMLInputElement).disabled).toBe(
      false,
    );
    await act(() => vi.advanceTimersByTimeAsync(1100));
    expect(screen.getByRole("alert").textContent).toMatch(/Wait 2 seconds/);
    for (let i = 0; i < 4 && reject.disabled; i++) {
      await act(() => vi.advanceTimersByTimeAsync(1000));
    }
    expect(reject.disabled).toBe(false);
    expect(screen.getByRole("alert").textContent).toMatch(/can now check the bank again/);
  });

  // PAY-502: payout_not_blocked shows in role="alert" and refetches the payouts.
  it("shows payout_not_blocked and refetches payouts", async () => {
    stubApi((c) =>
      c.method === "POST"
        ? apiError(
            409,
            "payout_not_blocked",
            "This payout isn't blocked any more. Refresh the page.",
          )
        : undefined,
    );
    const { client } = await open();
    const spy = vi.spyOn(client, "invalidateQueries");
    await userEvent.click(screen.getByRole("radio", { name: /Send again/ }));
    await userEvent.type(screen.getByLabelText(/Note for the audit log/), "n");
    await userEvent.click(submit());
    expect((await screen.findByRole("alert")).textContent).toMatch(/isn't blocked any more/);
    expect(spy).toHaveBeenCalledWith({ queryKey: ["pay-run", RUN_ID, "payouts"] });
  });

  // PAY-502: 422 details show inline on the note and the choice.
  it("shows 422 details inline", async () => {
    stubApi((c) =>
      c.method === "POST"
        ? {
            status: 422,
            body: {
              error: {
                code: "validation_error",
                message: "Some fields need attention.",
                request_id: "r",
                details: [
                  { field: "body.note", issue: "too_big" },
                  { field: "body.action", issue: "invalid_value" },
                ],
              },
            },
          }
        : undefined,
    );
    await open();
    await userEvent.click(screen.getByRole("radio", { name: /Send again/ }));
    await userEvent.type(screen.getByLabelText(/Note for the audit log/), "x");
    await userEvent.click(submit());
    expect(await screen.findByText("Enter a note of 1 to 500 characters.")).toBeTruthy();
    expect(screen.getByText("Choose what should happen.")).toBeTruthy();
    expect(screen.getByLabelText(/Note for the audit log/).getAttribute("aria-invalid")).toBe(
      "true",
    );
  });

  // PAY-NFR-14: Escape closes and focus returns to the trigger.
  it("closes on Escape", async () => {
    stubApi(() => undefined);
    await open();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Resolve" }));
  });
});
