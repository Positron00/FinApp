// @vitest-environment jsdom
import { randomBytes } from "node:crypto";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";

import { AcceptForm } from "@/components/forms/accept-form";
import { ResetFlow } from "@/components/forms/reset-forms";
import { readFragmentToken } from "@/lib/client/fragment-token";

import { apiError, stubApi } from "../helpers/ui";

// Generated at runtime so no literal looks like a credential (testing.md §9).
const TOKEN = randomBytes(24).toString("base64url");
const PASSWORD = "correct horse battery";

const assign = vi.fn();
const replaceState = vi.fn();

/** A fake location (jsdom's is unforgeable) with the given fragment. */
function at(path: string, hash = "") {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { origin: "http://127.0.0.1:4100", pathname: path, search: "", hash, assign },
  });
}

beforeEach(() => {
  assign.mockReset();
  replaceState.mockReset();
  vi.spyOn(window.history, "replaceState").mockImplementation(replaceState);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function setPassword(pw: string, confirm = pw) {
  await userEvent.type(screen.getByLabelText("New password"), pw);
  await userEvent.type(screen.getByLabelText("Confirm password"), confirm);
}

describe("readFragmentToken", () => {
  // PAY-103, PAY-NFR-07: the token comes from the fragment and is stripped from the address bar.
  it("reads #token and strips the fragment with replaceState", () => {
    at("/accept", `#token=${TOKEN}`);
    expect(readFragmentToken("/accept")).toBe(TOKEN);
    expect(replaceState).toHaveBeenCalledWith(null, "", "/accept");
  });

  // PAY-105: no fragment → null (the reset request form), and history is left alone.
  it("returns null without a fragment", () => {
    at("/reset");
    expect(readFragmentToken("/reset")).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });

  // PAY-103: a malformed token is "" (the invalid-link message), still stripped.
  it("returns an empty string for a malformed token", () => {
    at("/accept", "#token=short");
    expect(readFragmentToken("/accept")).toBe("");
    expect(replaceState).toHaveBeenCalled();
  });
});

describe("AcceptForm", () => {
  // PAY-103, PAY-NFR-07: the token is sent in the POST body only, never in a URL; then /dashboard.
  it("accepts with the fragment token and goes to the dashboard", async () => {
    at("/accept", `#token=${TOKEN}`);
    const calls = stubApi((c) =>
      c.path === "/api/auth/accept" ? { body: { user: { id: "u" } } } : undefined,
    );
    render(<AcceptForm />);
    await setPassword(PASSWORD);
    await userEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/dashboard"));
    expect(calls).toHaveLength(1);
    expect(calls[0]!.body).toEqual({ token: TOKEN, password: PASSWORD });
    expect(calls.every((c) => !c.path.includes(TOKEN))).toBe(true);
  });

  // PAY-103: the 12-character rule and the confirmation are checked before any request.
  it("refuses a short password and a mismatch on the client", async () => {
    at("/accept", `#token=${TOKEN}`);
    const calls = stubApi(() => undefined);
    render(<AcceptForm />);
    await setPassword("short");
    await userEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    expect(await screen.findByText("Use a password of at least 12 characters.")).toBeTruthy();
    // PAY-NFR-14: focus moves to the invalid field
    expect(document.activeElement).toBe(screen.getByLabelText("New password"));
    await userEvent.clear(screen.getByLabelText("New password"));
    await userEvent.type(screen.getByLabelText("New password"), PASSWORD);
    await userEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    expect(await screen.findByText("The passwords don't match.")).toBeTruthy();
    expect(screen.getByLabelText("Confirm password").getAttribute("aria-invalid")).toBe("true");
    expect(calls).toHaveLength(0);
  });

  // PAY-103: 400 invite_invalid shows its message with no form.
  it("shows invite_invalid without the form", async () => {
    at("/accept", `#token=${TOKEN}`);
    stubApi(() =>
      apiError(400, "invite_invalid", "This invite link has expired or was already used."),
    );
    render(<AcceptForm />);
    await setPassword(PASSWORD);
    await userEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/expired or was already used/);
    expect(screen.queryByLabelText("New password")).toBeNull();
  });

  // PAY-103: a missing token shows the invalid-link message at once.
  it("shows the invalid-link message without a token", async () => {
    at("/accept");
    render(<AcceptForm />);
    expect((await screen.findByRole("alert")).textContent).toMatch(/Ask your admin/);
  });

  // PAY-103: password_too_weak and network errors show in the alert.
  it("shows mapped and network errors", async () => {
    at("/accept", `#token=${TOKEN}`);
    let n = 0;
    stubApi(() =>
      ++n === 1
        ? apiError(422, "password_too_weak", "Use a password of at least 12 characters.")
        : new TypeError("Failed to fetch"),
    );
    render(<AcceptForm />);
    await setPassword(PASSWORD);
    await userEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/at least 12/);
    await userEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/temporarily unavailable/),
    );
  });
});

describe("useFragmentToken under StrictMode", () => {
  // PAY-103, PAY-105: StrictMode (next dev) runs the effect twice; the first run strips the
  // fragment, and the second must keep the token it read, not fall back to "no token".
  function strip() {
    replaceState.mockImplementation(() => {
      (window.location as { hash: string }).hash = "";
    });
  }

  it("keeps the invite token after the effect re-runs", async () => {
    at("/accept", `#token=${TOKEN}`);
    strip();
    const calls = stubApi((c) =>
      c.path === "/api/auth/accept" ? { body: { user: { id: "u" } } } : undefined,
    );
    render(
      <StrictMode>
        <AcceptForm />
      </StrictMode>,
    );
    await setPassword(PASSWORD);
    expect(screen.queryByRole("alert")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/dashboard"));
    expect(calls[0]!.body).toEqual({ token: TOKEN, password: PASSWORD });
    expect(window.location.hash).toBe("");
  });

  it("keeps the reset token after the effect re-runs", async () => {
    at("/reset", `#token=${TOKEN}`);
    strip();
    render(
      <StrictMode>
        <ResetFlow />
      </StrictMode>,
    );
    expect(await screen.findByLabelText("New password")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send reset link" })).toBeNull();
  });
});

describe("ResetFlow", () => {
  // PAY-105: no fragment → the request form; always the same notice (no enumeration).
  it("requests a reset and shows the neutral notice", async () => {
    at("/reset");
    const calls = stubApi((c) =>
      c.path === "/api/auth/reset/request" ? { status: 202, body: {} } : undefined,
    );
    render(<ResetFlow />);
    await userEvent.type(await screen.findByLabelText("Email"), " Someone@Example.com ");
    await userEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    expect((await screen.findByRole("status")).textContent).toBe(
      "If an account exists for that email, we've sent a reset link.",
    );
    expect(calls[0]!.body).toEqual({ email: "someone@example.com" });
  });

  // PAY-105, PAY-603: an invalid email is refused locally; 429 shows the Retry-After wait.
  it("validates the email and shows the rate limit", async () => {
    at("/reset");
    const calls = stubApi(() => ({
      ...apiError(429, "rate_limited", "Too many requests. Try again in 30 seconds."),
      headers: { "retry-after": "30" },
    }));
    render(<ResetFlow />);
    await userEvent.type(await screen.findByLabelText("Email"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByText("Enter a valid email address.")).toBeTruthy();
    expect(calls).toHaveLength(0);
    await userEvent.clear(screen.getByLabelText("Email"));
    await userEvent.type(screen.getByLabelText("Email"), "a@b.example");
    await userEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Too many requests. Try again in 30 seconds.",
    );
  });

  // PAY-105, R19: with #token, the confirm form; success goes to /login with the notice flag.
  it("confirms a reset with the fragment token", async () => {
    at("/reset", `#token=${TOKEN}`);
    const calls = stubApi((c) =>
      c.path === "/api/auth/reset/confirm" ? { status: 204 } : undefined,
    );
    render(<ResetFlow />);
    await screen.findByLabelText("New password");
    expect(replaceState).toHaveBeenCalledWith(null, "", "/reset");
    await setPassword(PASSWORD);
    await userEvent.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?reset=1"));
    expect(calls[0]!.body).toEqual({ token: TOKEN, password: PASSWORD });
  });

  // PAY-105: reset_invalid replaces the form with its message and a link to request a new one.
  it("shows reset_invalid with a link to start again", async () => {
    at("/reset", `#token=${TOKEN}`);
    stubApi(() => apiError(400, "reset_invalid", "This reset link has expired."));
    render(<ResetFlow />);
    await setPassword(PASSWORD);
    await userEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/Request a new one/);
    expect(screen.getByRole("link", { name: "Request a new link" }).getAttribute("href")).toBe(
      "/reset",
    );
  });

  // PAY-105: other errors keep the form and show the message.
  it("keeps the form on other errors", async () => {
    at("/reset", `#token=${TOKEN}`);
    stubApi(() => apiError(503, "service_unavailable", "The service is temporarily unavailable."));
    render(<ResetFlow />);
    await setPassword(PASSWORD);
    await userEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/temporarily unavailable/);
    expect(screen.getByLabelText("New password")).toBeTruthy();
  });
});

describe("public pages", () => {
  // PAY-103, PAY-105, PAY-NFR-14: each public page has one h1 and its form.
  it("render their headings", async () => {
    at("/accept");
    const { default: AcceptPage } = await import("@/app/(public)/accept/page");
    const { default: ResetPage } = await import("@/app/(public)/reset/page");
    const { default: LoginPage } = await import("@/app/(public)/login/page");
    render(<AcceptPage />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Accept your invite");
    cleanup();
    render(<ResetPage />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Reset your password");
    cleanup();
    render(await LoginPage({ searchParams: Promise.resolve({ reset: "1" }) }));
    expect(screen.getByRole("status").textContent).toBe(
      "Password changed. Sign in with your new password.",
    );
    expect(screen.getByRole("link", { name: "Forgot password?" }).getAttribute("href")).toBe(
      "/reset",
    );
  });
});
