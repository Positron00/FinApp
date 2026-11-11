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
function Harness({
  onResolved,
  target = blocked,
}: {
  onResolved: (m: string) => void;
  target?: typeof blocked;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Resolve
      </button>
      {open ? (
        <ResolveDialog
          payout={target as never}
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

async function open(target = blocked) {
  const onResolved = vi.fn();
  const r = renderWithClient(<Harness onResolved={onResolved} target={target} />);
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

  // C73: an expired bank key → "Send again" disabled with the reason; the warning asks for the
  // bank statement (runbook) instead of a key lookup; "Mark rejected" still works.
  it("disables Send again for a key_window_expired attempt", async () => {
    const calls = stubApi((c) => (c.method === "POST" ? { body: blocked } : undefined));
    const expired = payout({
      status: "requested",
      attempts: [
        attempt({
          status: "needs_operator",
          failure_reason: "key_window_expired",
          final_at: null,
          bank_reference: null,
        }),
      ],
    });
    const { onResolved } = await open(expired as typeof blocked);
    const dialog = screen.getByRole("alertdialog", { name: "Resolve blocked payout" });
    const redrive = screen.getByRole("radio", { name: /Send again/ }) as HTMLInputElement;
    expect(redrive.disabled).toBe(true);
    expect(redrive.getAttribute("aria-describedby")).toBeTruthy();
    expect(document.getElementById(redrive.getAttribute("aria-describedby")!)!.textContent).toMatch(
      /^Not available: the bank key for this payment has expired/,
    );
    expect(dialog.textContent).toMatch(/Check the bank statement/);
    expect(dialog.textContent).toContain("PayoutNeedsOperator.md");
    expect(dialog.textContent).not.toContain("GET /v1/transfers/{key}");
    // focus still starts on the warning
    expect(document.activeElement).toBe(
      screen.getByRole("heading", { name: "Check the bank first" }),
    );
    await userEvent.click(redrive);
    expect(redrive.checked).toBe(false);
    await userEvent.click(screen.getByRole("radio", { name: /Mark rejected/ }));
    await userEvent.type(screen.getByLabelText(/Note for the audit log/), "statement ref ST-9");
    await userEvent.click(submit());
    await waitFor(() => expect(onResolved).toHaveBeenCalledWith("Payout marked rejected"));
    expect(calls[0]).toMatchObject({ body: { action: "mark_rejected" } });
  });

  // C73: other blocked reasons keep Send again and the key-lookup advice.
  it("keeps Send again for other needs_operator reasons", async () => {
    stubApi(() => undefined);
    const poison = payout({
      status: "requested",
      attempts: [
        attempt({ status: "needs_operator", failure_reason: "schema_invalid", final_at: null }),
      ],
    });
    await open(poison as typeof blocked);
    expect((screen.getByRole("radio", { name: /Send again/ }) as HTMLInputElement).disabled).toBe(
      false,
    );
    expect(screen.getByRole("alertdialog").textContent).toContain("GET /v1/transfers/{key}");
  });

  // C73: a stale dialog (the attempt's key expired after it loaded) → 422 redrive_not_allowed
  // shows a readable alert, not the generic field error, and Send again is disabled from then.
  it("maps 422 redrive_not_allowed to a readable message", async () => {
    stubApi((c) =>
      c.method === "POST"
        ? {
            status: 422,
            body: {
              error: {
                code: "validation_error",
                message: "Some fields need attention.",
                request_id: "req-422",
                details: [{ field: "body.action", issue: "redrive_not_allowed" }],
              },
            },
          }
        : undefined,
    );
    const { onResolved } = await open();
    await userEvent.click(screen.getByRole("radio", { name: /Send again/ }));
    await userEvent.type(screen.getByLabelText(/Note for the audit log/), "bank: not found");
    await userEvent.click(submit());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(
      "Send again isn't possible for this payment: its bank key has expired. Check the bank statement, then mark it rejected.",
    );
    expect(alert.textContent).toContain("Reference: req-422");
    expect(screen.queryByText("Choose what should happen.")).toBeNull();
    const redrive = screen.getByRole("radio", { name: /Send again/ }) as HTMLInputElement;
    expect(redrive.disabled).toBe(true);
    expect(redrive.checked).toBe(false);
    expect(submit().disabled).toBe(true); // no choice left
    expect(screen.getByRole("alertdialog").textContent).toMatch(/Check the bank statement/);
    expect(onResolved).not.toHaveBeenCalled();
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
