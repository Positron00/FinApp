// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConfirmDialog } from "@/components/dialog";
import { RoleGate } from "@/components/role-gate";

afterEach(cleanup);

describe("RoleGate", () => {
  // PAY-601: children only for a held role (any one suffices); the BFF still enforces.
  it("shows children only to users with one of the roles", () => {
    const { rerender } = render(
      <RoleGate held={["approver"]} roles={["payroll_admin"]}>
        <button type="button">Calculate</button>
      </RoleGate>,
    );
    expect(screen.queryByRole("button", { name: "Calculate" })).toBeNull();
    rerender(
      <RoleGate held={["approver", "payroll_admin"]} roles={["admin", "payroll_admin"]}>
        <button type="button">Calculate</button>
      </RoleGate>,
    );
    expect(screen.getByRole("button", { name: "Calculate" })).toBeTruthy();
  });
});

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
