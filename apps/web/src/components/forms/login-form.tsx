"use client";

import { useEffect, useState, type FormEvent } from "react";

import { ApiError, apiFetch } from "@/lib/client/api";
import { LoginBody } from "@/lib/schemas/requests";

/**
 * The open-redirect guard (web LLD §6.2, C63): `next` must be a path that resolves to this
 * origin. Control characters and backslashes are refused outright, because the URL parser
 * strips tab/CR/LF and treats `\` as `/` (so "/\t/evil" would become "//evil").
 */
export function safeNext(
  next: string | undefined,
  origin: string = typeof window === "undefined" ? "http://invalid" : window.location.origin,
): string {
  const fallback = "/dashboard";
  if (!next || !next.startsWith("/") || /[\u0000-\u001f\u007f\\]/.test(next)) return fallback;
  let u: URL;
  try {
    u = new URL(next, origin);
  } catch {
    return fallback;
  }
  if (u.origin !== new URL(origin).origin) return fallback;
  return `${u.pathname}${u.search}`;
}

/** The sign-in form (web LLD §6.2, PAY-104). */
export function LoginForm({ next }: { next?: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [wait, setWait] = useState(0);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (wait <= 0) return;
    const t = setTimeout(() => setWait((w) => w - 1), 1000);
    return () => clearTimeout(t);
  }, [wait]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const parsed = LoginBody.safeParse({ email, password });
    if (!parsed.success) {
      setError("Enter your email and password.");
      return;
    }
    setPending(true);
    try {
      await apiFetch("/api/auth/login", {
        method: "POST",
        body: parsed.data,
        noAuthRedirect: true,
      });
      window.location.assign(safeNext(next));
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setWait(err.retryAfter ?? 60);
        setError(null);
      } else if (err instanceof ApiError && err.status === 503) {
        setError("The service is temporarily unavailable. Please try again.");
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("The service is temporarily unavailable. Please try again.");
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="mt-6 flex flex-col gap-4" onSubmit={(e) => void onSubmit(e)} noValidate>
      <label className="flex flex-col gap-1">
        <span>Email</span>
        <input
          className="rounded border border-gray-400 p-2"
          type="email"
          name="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          aria-invalid={error !== null}
          aria-describedby={error ? "login-error" : undefined}
        />
      </label>
      <label className="flex flex-col gap-1">
        <span>Password</span>
        <input
          className="rounded border border-gray-400 p-2"
          type="password"
          name="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-invalid={error !== null}
          aria-describedby={error ? "login-error" : undefined}
        />
      </label>
      {error ? (
        <p id="login-error" role="alert" className="text-danger">
          {error}
        </p>
      ) : null}
      {wait > 0 ? (
        <p role="alert" className="text-danger">
          Too many requests. Try again in {wait} seconds.
        </p>
      ) : null}
      <button
        type="submit"
        className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
        disabled={pending || wait > 0}
      >
        Sign in
      </button>
    </form>
  );
}
