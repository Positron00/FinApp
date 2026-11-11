"use client";

import { useState, type FormEvent } from "react";

import { TextField } from "@/components/field";
import { Password } from "@/lib/schemas/common";

/**
 * New password and confirmation (at least 12 characters; PAY-103, PAY-105). Shared by the
 * accept and reset-confirm forms. The BFF and identity check again; this is instant feedback.
 */
export function PasswordSetForm({
  submitLabel,
  pending,
  error,
  onSubmit,
}: {
  submitLabel: string;
  pending: boolean;
  error: string | null;
  onSubmit: (password: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [fieldError, setFieldError] = useState<{ field: "password" | "confirm"; msg: string }>();

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    let fe: typeof fieldError;
    if (!Password.safeParse(password).success) {
      fe = { field: "password", msg: "Use a password of at least 12 characters." };
    } else if (password !== confirm) {
      fe = { field: "confirm", msg: "The passwords don't match." };
    }
    setFieldError(fe);
    if (fe) {
      // Focus the first invalid field after a failed submit (PAY-NFR-14).
      form.querySelector<HTMLInputElement>(`[name="${fe.field}"]`)?.focus();
      return;
    }
    onSubmit(password);
  }

  const pwErr = fieldError?.field === "password" ? fieldError.msg : null;
  const cfErr = fieldError?.field === "confirm" ? fieldError.msg : null;
  return (
    <form className="mt-6 flex flex-col gap-4" onSubmit={submit} noValidate>
      <TextField
        label="New password"
        name="password"
        type="password"
        autoComplete="new-password"
        required
        minLength={12}
        value={password}
        onChange={setPassword}
        hint="At least 12 characters."
        error={pwErr ?? undefined}
      />
      <TextField
        label="Confirm password"
        name="confirm"
        type="password"
        autoComplete="new-password"
        required
        value={confirm}
        onChange={setConfirm}
        error={cfErr ?? undefined}
      />
      {error ? (
        <p role="alert" className="text-danger">
          {error}
        </p>
      ) : null}
      <button
        type="submit"
        className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
        disabled={pending}
      >
        {submitLabel}
      </button>
    </form>
  );
}
