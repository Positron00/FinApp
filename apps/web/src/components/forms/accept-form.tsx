"use client";

import { useState } from "react";

import { ApiError, apiFetch } from "@/lib/client/api";
import { errorText } from "@/lib/client/form-errors";
import { useFragmentToken } from "@/lib/client/fragment-token";

import { PasswordSetForm } from "./password-set-form";

const INVALID =
  "This invite link has expired or was already used. Ask your admin to send a new one.";

/**
 * Accept an invite (web LLD §6.2 `/accept`, PAY-103). The token comes from `#token=…`, is kept in
 * state and stripped from the address bar on mount; it's sent only in the POST body. 400
 * `invite_invalid` shows its message with no form.
 */
export function AcceptForm() {
  const token = useFragmentToken("/accept");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);

  if (token === undefined) return <p>Loading…</p>;
  if (!token || invalid) {
    return (
      <p role="alert" className="mt-6 text-danger">
        {INVALID}
      </p>
    );
  }

  async function submit(password: string) {
    setPending(true);
    setError(null);
    try {
      await apiFetch("/api/auth/accept", {
        method: "POST",
        body: { token, password },
        noAuthRedirect: true,
      });
      // A full navigation: the new session cookie is in place for the server components.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign("/dashboard");
    } catch (err) {
      if (err instanceof ApiError && err.code === "invite_invalid") setInvalid(true);
      else setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <PasswordSetForm
      submitLabel="Set password and sign in"
      pending={pending}
      error={error}
      onSubmit={(p) => void submit(p)}
    />
  );
}
