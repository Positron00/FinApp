// @vitest-environment jsdom
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { UsersPage } from "@/components/users/users-page";

import { INVITE_ID, pendingInvite, TARGET_USER_ID, user } from "../helpers/fixtures";
import { USER_ID } from "../helpers/request";
import { apiError, renderWithClient, stubApi, type Call } from "../helpers/ui";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ACTIVE_ID = "0193a000-0000-7000-8000-0000000000a4";
const INACTIVE_ID = "0193a000-0000-7000-8000-0000000000a5";

const USERS = [
  user({
    id: USER_ID,
    name: "Olive Owner",
    status: "active",
    roles: ["admin"],
    pending_invite_id: null,
  }),
  user(), // Pat Pending, pending, with pending_invite_id
  user({ id: ACTIVE_ID, name: "Ann Active", status: "active", pending_invite_id: null }),
  user({ id: INACTIVE_ID, name: "Ian Inactive", status: "inactive", pending_invite_id: null }),
];

function api(over: (c: Call) => ReturnType<Parameters<typeof stubApi>[0]> = () => undefined) {
  return stubApi(
    (c) =>
      over(c) ?? (c.method === "GET" ? { body: { items: USERS, next_cursor: null } } : undefined),
  );
}

describe("UsersPage", () => {
  // PAY-108, C25: the table; no Reactivate anywhere; inactive users are read-only; own row disabled.
  it("renders rows with the right controls", async () => {
    api();
    renderWithClient(<UsersPage meId={USER_ID} />);
    await screen.findByText("Ann Active");
    expect(screen.getByText("Olive Owner (you)")).toBeTruthy();
    expect(screen.queryByText(/Reactivate/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Reactivate/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit roles for Ian Inactive" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deactivate Ian Inactive" })).toBeNull();
    // cannot_modify_self: the admin's own row is disabled
    expect(
      (screen.getByRole("button", { name: "Edit roles for Olive Owner" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Deactivate Olive Owner" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    // a pending user can't be deactivated, only resent
    expect(screen.queryByRole("button", { name: "Deactivate Pat Pending" })).toBeNull();
    expect(screen.getByRole("button", { name: "Resend invite to Pat Pending" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Resend invite to Ann Active" })).toBeNull();
  });

  // PAY-102, R21, C11: Resend posts to /api/invites/<pending_invite_id>/resend.
  it("resends by pending_invite_id", async () => {
    const calls = api((c) => (c.method === "POST" ? { body: pendingInvite() } : undefined));
    renderWithClient(<UsersPage meId={USER_ID} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Resend invite to Pat Pending" }),
    );
    await screen.findByText("Invite sent again.");
    expect(calls.find((c) => c.method === "POST")!.path).toBe(`/api/invites/${INVITE_ID}/resend`);
  });

  // PAY-102: invite_not_pending shows its message in an alert.
  it("shows invite_not_pending", async () => {
    api((c) =>
      c.method === "POST"
        ? apiError(409, "invite_not_pending", "This user has already accepted their invite.")
        : undefined,
    );
    renderWithClient(<UsersPage meId={USER_ID} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Resend invite to Pat Pending" }),
    );
    expect((await screen.findByRole("alert")).textContent).toContain("already accepted");
  });

  // PAY-101: the invite dialog requires a role client-side, then posts InviteBody.
  it("invites a user", async () => {
    const calls = api((c) => (c.method === "POST" ? { status: 201, body: user() } : undefined));
    renderWithClient(<UsersPage meId={USER_ID} />);
    await userEvent.click(await screen.findByRole("button", { name: "Invite user" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^Email/), "New@Payrun.Example.com");
    await userEvent.type(within(dialog).getByLabelText(/^Name/), "Nia New");
    await userEvent.click(within(dialog).getByRole("button", { name: "Send invite" }));
    expect(await within(dialog).findByText("Choose at least one role.")).toBeTruthy();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
    await userEvent.click(within(dialog).getByLabelText("Payroll admin"));
    await userEvent.click(within(dialog).getByLabelText("Approver"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Send invite" }));
    await screen.findByText("Invite sent to pat@payrun.example.com.");
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe("/api/users/invite");
    expect(post.body).toEqual({
      email: "new@payrun.example.com",
      name: "Nia New",
      roles: ["payroll_admin", "approver"],
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  // PAY-101: 409 user_email_taken shows on the email field.
  it("shows user_email_taken on the email field", async () => {
    api((c) =>
      c.method === "POST"
        ? apiError(409, "user_email_taken", "A user with this email already exists.")
        : undefined,
    );
    renderWithClient(<UsersPage meId={USER_ID} />);
    await userEvent.click(await screen.findByRole("button", { name: "Invite user" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^Email/), "pat@payrun.example.com");
    await userEvent.type(within(dialog).getByLabelText(/^Name/), "Pat");
    await userEvent.click(within(dialog).getByLabelText("Admin"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Send invite" }));
    await within(dialog).findByText("A user with this email already exists.");
    expect(
      within(dialog)
        .getByLabelText(/^Email/)
        .getAttribute("aria-invalid"),
    ).toBe("true");
    // PAY-NFR-14: focus is on the Email field as soon as the error renders.
    expect(document.activeElement).toBe(within(dialog).getByLabelText(/^Email/));
    expect(within(dialog).queryByRole("alert")).toBeNull();
  });

  // PAY-107, PAY-108: Edit roles PATCHes {roles}; the notice says they'll sign in again.
  it("edits roles", async () => {
    const calls = api((c) =>
      c.method === "PATCH" ? { body: user({ id: ACTIVE_ID, roles: ["approver"] }) } : undefined,
    );
    renderWithClient(<UsersPage meId={USER_ID} />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit roles for Ann Active" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByLabelText("Payroll admin")); // uncheck
    await userEvent.click(within(dialog).getByRole("button", { name: "Save roles" }));
    expect(within(dialog).getByText("Choose at least one role.")).toBeTruthy();
    await userEvent.click(within(dialog).getByLabelText("Approver"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save roles" }));
    await screen.findByText(/sign in again/);
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.path).toBe(`/api/users/${ACTIVE_ID}`);
    expect(patch.body).toEqual({ roles: ["approver"] });
  });

  // PAY-108: last_admin and cannot_modify_self show in role="alert" inside the dialog.
  it.each([
    ["last_admin", "The company must keep at least one active admin."],
    ["cannot_modify_self", "You can't change your own roles or status. Ask another admin."],
  ])("shows %s", async (code, message) => {
    api((c) => (c.method === "PATCH" ? apiError(409, code, message) : undefined));
    renderWithClient(<UsersPage meId={USER_ID} />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit roles for Ann Active" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save roles" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain(message);
  });

  // PAY-108, C25: Deactivate is a confirm that PATCHes {status: "inactive"}.
  it("deactivates after a confirm", async () => {
    const calls = api((c) =>
      c.method === "PATCH" ? { body: user({ id: ACTIVE_ID, status: "inactive" }) } : undefined,
    );
    renderWithClient(<UsersPage meId={USER_ID} />);
    await userEvent.click(await screen.findByRole("button", { name: "Deactivate Ann Active" }));
    const dialog = screen.getByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Deactivate" }));
    await screen.findByText("Ann Active was deactivated.");
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ status: "inactive" });
  });

  // PAY-NFR-14: focus starts in the dialog, Tab is trapped, Escape closes, focus returns.
  it("traps focus and returns it to the trigger", async () => {
    api();
    renderWithClient(<UsersPage meId={USER_ID} />);
    const trigger = await screen.findByRole("button", { name: "Invite user" });
    await userEvent.click(trigger);
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("aria-labelledby")).toBeTruthy();
    const email = within(dialog).getByLabelText(/^Email/);
    expect(document.activeElement).toBe(email);
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Cancel" }));
    await userEvent.tab();
    expect(document.activeElement).toBe(email);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  // PAY-108, C8: the pager follows next_cursor.
  it("pages by cursor", async () => {
    const calls = stubApi((c) =>
      c.path.includes("cursor=u2")
        ? { body: { items: [user({ id: TARGET_USER_ID, name: "Zed Last" })], next_cursor: null } }
        : { body: { items: USERS, next_cursor: "u2" } },
    );
    renderWithClient(<UsersPage meId={USER_ID} />);
    await userEvent.click(await screen.findByRole("button", { name: "Next" }));
    await screen.findByText("Zed Last");
    expect(calls.at(-1)!.path).toBe("/api/users?limit=50&cursor=u2");
  });
});
