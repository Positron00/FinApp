"use client";

import { useId, type InputHTMLAttributes, type ReactNode } from "react";

/**
 * A labelled text input with its hint and inline error (PAY-NFR-14): the error sets
 * `aria-invalid` and is linked with `aria-describedby`, as is the hint. Both sit outside the
 * `<label>`, so the accessible name is the label alone.
 */
export function TextField({
  label,
  name,
  value,
  onChange,
  error,
  hint,
  ...rest
}: {
  label: string;
  name: string;
  value: string;
  onChange: (v: string) => void;
  error?: string;
  hint?: ReactNode;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "name">) {
  const id = useId();
  const described = [hint ? `${id}-hint` : null, error ? `${id}-err` : null].filter(Boolean);
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={`${id}-input`}>{label}</label>
      <input
        id={`${id}-input`}
        className="rounded border border-gray-400 p-2"
        name={name}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={error ? true : undefined}
        aria-describedby={described.length > 0 ? described.join(" ") : undefined}
        {...rest}
      />
      {hint ? (
        <span id={`${id}-hint`} className="text-sm text-muted">
          {hint}
        </span>
      ) : null}
      {error ? (
        <span id={`${id}-err`} className="text-danger">
          {error}
        </span>
      ) : null}
    </div>
  );
}

/** A labelled select with an inline error. */
export function SelectField<T extends string>({
  label,
  name,
  value,
  onChange,
  options,
  error,
  disabled,
}: {
  label: string;
  name: string;
  value: T;
  onChange: (v: T) => void;
  options: readonly { value: T; label: string }[];
  error?: string;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={`${id}-input`}>{label}</label>
      <select
        id={`${id}-input`}
        className="rounded border border-gray-400 p-2"
        name={name}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value as T)}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-err` : undefined}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {error ? (
        <span id={`${id}-err`} className="text-danger">
          {error}
        </span>
      ) : null}
    </div>
  );
}

/** An error with its reference, for forms and dialogs (`role="alert"`). */
export function FormError({
  message,
  requestId,
}: {
  message: string | null;
  requestId?: string | null;
}) {
  if (!message) return null;
  return (
    <div role="alert" className="text-danger">
      <p>{message}</p>
      {requestId ? <p className="text-sm">Reference: {requestId}</p> : null}
    </div>
  );
}
