"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";

import { apiFetch } from "@/lib/client/api";
import type { Role } from "@/lib/roles";

export interface ShellUser {
  id: string;
  name: string;
  roles: Role[];
}

const NAV: { href: string; label: string; roles?: Role[] }[] = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/pay-runs", label: "Pay runs" },
];

/** Layout, skip link, role-filtered navigation and sign-out (web LLD §1.4). */
export function AppShell({ user, children }: { user: ShellUser; children: ReactNode }) {
  const [signingOut, setSigningOut] = useState(false);

  async function signOut() {
    setSigningOut(true);
    try {
      await apiFetch("/api/auth/logout", { method: "POST", noAuthRedirect: true });
    } finally {
      // A full navigation on purpose: it drops every client cache of the signed-out user.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign("/login");
    }
  }

  return (
    <>
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:p-2">
        Skip to content
      </a>
      <header className="border-b border-gray-200">
        <div className="mx-auto flex max-w-5xl items-center justify-between p-4">
          <nav aria-label="Main">
            <ul className="flex gap-4">
              {NAV.filter((n) => !n.roles || n.roles.some((r) => user.roles.includes(r))).map(
                (n) => (
                  <li key={n.href}>
                    <Link className="text-brand underline" href={n.href}>
                      {n.label}
                    </Link>
                  </li>
                ),
              )}
            </ul>
          </nav>
          <div className="flex items-center gap-3 text-sm">
            <span>{user.name}</span>
            <button
              type="button"
              className="rounded border border-gray-300 px-3 py-1"
              onClick={() => void signOut()}
              disabled={signingOut}
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-5xl p-4">
        {children}
      </main>
    </>
  );
}
