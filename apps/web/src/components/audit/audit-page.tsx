"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { CursorPager, useCursorStack } from "@/components/cursor-pager";
import { ErrorBanner } from "@/components/error-banner";
import { TabList, TabPanel } from "@/components/tabs";
import { apiFetch } from "@/lib/client/api";
import { listQuery, queryKeys, type PageOf } from "@/lib/client/query-keys";
import type { AuthAuditEvent, PayrollAuditEvent } from "@/lib/schemas/responses";

type Tab = "auth" | "payroll";
const TABS = [
  { id: "auth", label: "Sign-in and users" },
  { id: "payroll", label: "Payroll" },
] as const satisfies readonly { id: Tab; label: string }[];

/** `details` shows its key names only, never values (web LLD §6.2, PAY-NFR-07). */
export function detailKeys(details: Record<string, unknown>): string {
  const keys = Object.keys(details);
  return keys.length > 0 ? keys.join(", ") : "—";
}

const short = (id: string | null) => (id ? id.slice(0, 8) : "—");

/**
 * `/audit` (web LLD §6.2; PAY-109, plan §8): admin. Two WAI-ARIA tabs, each with its own cursor
 * pager (C8): the identity auth log and the payroll audit events, newest first.
 */
export function AuditPage() {
  const [tab, setTab] = useState<Tab>("auth");
  return (
    <>
      <h1 className="text-2xl font-semibold">Audit log</h1>
      <TabList tabs={TABS} active={tab} onSelect={setTab} label="Audit logs" />
      <TabPanel id="auth" active={tab === "auth"}>
        <AuthAuditTable />
      </TabPanel>
      <TabPanel id="payroll" active={tab === "payroll"}>
        <PayrollAuditTable />
      </TabPanel>
    </>
  );
}

function useAuditPage<T>(path: string, key: (c: string | null) => readonly unknown[]) {
  const pager = useCursorStack();
  const q = useQuery({
    queryKey: key(pager.cursor),
    queryFn: () => apiFetch<PageOf<T>>(`${path}?${listQuery(50, pager.cursor)}`),
  });
  return { pager, q };
}

export function AuthAuditTable() {
  const { pager, q } = useAuditPage<AuthAuditEvent>("/api/audit/auth", queryKeys.authAudit);
  if (q.isError) return <ErrorBanner error={q.error} onRetry={() => void q.refetch()} />;
  if (!q.data) return <p className="mt-3">Loading…</p>;
  return (
    <>
      <table className="mt-3 w-full text-left">
        <caption className="sr-only">Sign-in and user events, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col">Event</th>
            <th scope="col">Actor</th>
            <th scope="col">Target</th>
            <th scope="col">IP hash</th>
            <th scope="col">Details</th>
          </tr>
        </thead>
        <tbody>
          {q.data.items.map((e) => (
            <tr key={e.id} className="border-t">
              <td>{new Date(e.at).toLocaleString()}</td>
              <td>{e.event}</td>
              <td>{short(e.actor_user_id)}</td>
              <td>{short(e.target_user_id)}</td>
              <td>{e.ip_hash ? e.ip_hash.slice(0, 8) : "—"}</td>
              <td>{detailKeys(e.details)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <CursorPager
        nextCursor={q.data.next_cursor}
        hasPrevious={pager.hasPrevious}
        onNext={pager.next}
        onPrevious={pager.previous}
      />
    </>
  );
}

export function PayrollAuditTable() {
  const { pager, q } = useAuditPage<PayrollAuditEvent>(
    "/api/audit/payroll",
    queryKeys.payrollAudit,
  );
  if (q.isError) return <ErrorBanner error={q.error} onRetry={() => void q.refetch()} />;
  if (!q.data) return <p className="mt-3">Loading…</p>;
  return (
    <>
      <table className="mt-3 w-full text-left">
        <caption className="sr-only">Payroll events, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col">Event</th>
            <th scope="col">Actor</th>
            <th scope="col">Entity</th>
            <th scope="col">Entity ID</th>
            <th scope="col">Details</th>
          </tr>
        </thead>
        <tbody>
          {q.data.items.map((e) => (
            <tr key={e.id} className="border-t">
              <td>{new Date(e.at).toLocaleString()}</td>
              <td>{e.event}</td>
              <td>{e.actor_id.startsWith("service:") ? e.actor_id : short(e.actor_id)}</td>
              <td>{e.entity_type}</td>
              <td>{short(e.entity_id)}</td>
              <td>{detailKeys(e.details)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <CursorPager
        nextCursor={q.data.next_cursor}
        hasPrevious={pager.hasPrevious}
        onNext={pager.next}
        onPrevious={pager.previous}
      />
    </>
  );
}
