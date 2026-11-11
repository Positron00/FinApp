// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";

import { ConfirmDialog, Dialog } from "@/components/dialog";

afterEach(cleanup);

describe("ConfirmDialog", () => {
  // PAY-NFR-14: an alertdialog; Cancel gets the first focus; confirm and Escape call back.
  it("is an accessible destructive confirm", async () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      <ConfirmDialog title="Delete?" confirmLabel="Delete" onConfirm={onConfirm} onClose={onClose}>
        <p>Gone for good.</p>
      </ConfirmDialog>,
    );
    const dialog = screen.getByRole("alertdialog", { name: "Delete?" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledOnce();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
  });
});

/** A dialog whose initial focus is a heading outside the tab order, as in the resolve dialog. */
function HeadingFirst() {
  const ref = useRef<HTMLHeadingElement>(null);
  return (
    <>
      <button type="button">Behind</button>
      <Dialog title="Check" onClose={() => undefined} alert initialFocus={ref}>
        <h3 ref={ref} tabIndex={-1}>
          Warning
        </h3>
        <button type="button">First</button>
        <button type="button">Last</button>
      </Dialog>
    </>
  );
}

describe("Dialog focus trap", () => {
  // PAY-NFR-14: Tab and Shift+Tab wrap at both ends.
  it("wraps Tab from the last item and Shift+Tab from the first", async () => {
    render(<HeadingFirst />);
    const first = screen.getByRole("button", { name: "First" });
    const last = screen.getByRole("button", { name: "Last" });
    last.focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(first);
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(last);
  });

  // PAY-NFR-14: focus on a non-tabbable element (the initial-focus heading) can't leave the
  // dialog either way: Shift+Tab goes to the last item, Tab to the first.
  it("keeps focus inside from a non-tabbable initial focus", async () => {
    render(<HeadingFirst />);
    const heading = screen.getByRole("heading", { name: "Warning" });
    expect(document.activeElement).toBe(heading);
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Last" }));
    heading.focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "First" }));
    // the dialog container itself holding focus behaves the same
    screen.getByRole("alertdialog", { name: "Check" }).focus();
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Last" }));
    expect(document.activeElement).not.toBe(screen.getByRole("button", { name: "Behind" }));
  });
});
