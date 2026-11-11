"use client";

import { useState, type FormEvent } from "react";

import { TextField } from "@/components/field";
import { ApiError, apiFetch } from "@/lib/client/api";
import { errorText } from "@/lib/client/form-errors";
import { useFragmentToken } from "@/lib/client/fragment-token";
import { ResetRequestBody } from "@/lib/schemas/requests";

import { PasswordSetForm } from "./password-set-form";

export const RESET_SENT = "If an account exists for that email, we've sent a reset link.";
const INVALID = "This reset link has expired or was already used. Request a new one.";

/**
 * `/reset` (web LLD §6.2, PAY-105): with no `#token`, the request form; with one, the confirm
 * form. The token is read from the fragment and stripped on mount, as on `/accept`.
 */
export function ResetFlow() {
  const token = useFragmentToken("/reset");
  if (token === undefined) return <p>Loading…</p>;
  if (token === null) return <ResetRequestForm />;
  return <ResetConfirmForm token={token} />;
}

/** Always answers with the same notice, whatever the email (no account enumeration). */
export function ResetRequestForm() {
  const [email, setEmail] = useState("");
  const [pending, setPending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [wait, setWait] = useState<number | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const parsed = ResetRequestBody.safeParse({ email });
    if (!parsed.success) {
      setFieldError("Enter a valid email address.");
      form.querySelector<HTMLInputElement>('[name="email"]')?.focus();
      return;
    }
    setFieldError(null);
    setError(null);
    setPending(true);
    try {
      await apiFetch("/api/auth/reset/request", {
        method: "POST",
        body: parsed.data,
        noAuthRedirect: true,
      });
      setSent(true);
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) setWait(err.retryAfter ?? 60);
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  if (sent) {
    return (
      <p role="status" className="mt-6">
        {RESET_SENT}
      </p>
    );
  }
  return (
    <form className="mt-6 flex flex-col gap-4" onSubmit={(e) => void submit(e)} noValidate>
      <TextField
        label="Email"
        name="email"
        type="email"
        autoComplete="username"
        required
        value={email}
        onChange={setEmail}
        error={fieldError ?? undefined}
      />
      {error ? (
        <p role="alert" className="text-danger">
          {wait !== null ? `Too many requests. Try again in ${wait} seconds.` : error}
        </p>
      ) : null}
      <button
        type="submit"
        className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
        disabled={pending}
      >
        Send reset link
      </button>
    </form>
  );
}

/** Sets the new password; on success every session of the user has ended (R19). */
export function ResetConfirmForm({ token }: { token: string }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(token === "");

  if (invalid) {
    return (
      <p role="alert" className="mt-6 text-danger">
        {INVALID}{" "}
        <a className="text-brand underline" href="/reset">
          Request a new link
        </a>
      </p>
    );
  }

  async function submit(password: string) {
    setPending(true);
    setError(null);
    try {
      await apiFetch("/api/auth/reset/confirm", {
        method: "POST",
        body: { token, password },
        noAuthRedirect: true,
      });
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign("/login?reset=1");
    } catch (err) {
      if (err instanceof ApiError && err.code === "reset_invalid") setInvalid(true);
      else setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <PasswordSetForm
      submitLabel="Change password"
      pending={pending}
      error={error}
      onSubmit={(p) => void submit(p)}
    />
  );
}
