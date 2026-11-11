"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, useState, type FormEvent } from "react";

import { ConfirmDialog } from "@/components/dialog";
import { ErrorBanner } from "@/components/error-banner";
import { FormError, SelectField, TextField } from "@/components/field";
import { ApiError, apiFetch } from "@/lib/client/api";
import {
  apiFieldErrors,
  errorText,
  focusFirstInvalid,
  zodFieldErrors,
  type FieldErrors,
} from "@/lib/client/form-errors";
import { queryKeys } from "@/lib/client/query-keys";
import { bpsToInput, formatBps, parsePercent } from "@/lib/money";
import { SettingsBody, timeZones } from "@/lib/schemas/requests";
import type { Settings } from "@/lib/schemas/responses";

interface Values {
  name: string;
  tax: string;
  retirement: string;
  timezone: string;
  biweekly_anchor: string;
  require_dual_approval: boolean;
}

const toValues = (s: Settings): Values => ({
  name: s.name,
  tax: bpsToInput(s.tax_rate_bps),
  retirement: bpsToInput(s.retirement_bps),
  timezone: s.timezone,
  biweekly_anchor: s.biweekly_anchor,
  require_dual_approval: s.require_dual_approval,
});

/** "you" for the current user, else the first 8 characters of the ID (§6.1). */
function who(id: string | null, meId: string): string {
  if (id === null) return "—";
  return id === meId ? "you" : id.slice(0, 8);
}

/**
 * `/settings` (web LLD §6.2; PAY-200, ADR 0005): read-only for non-admins; the admin form edits
 * the name, rates (entered as percent, sent as basis points with integer arithmetic), time
 * zone, biweekly anchor (Mondays) and dual approval. Turning dual approval off asks first.
 * 409 `anchor_locked` shows on the anchor field; 404 `company_settings_not_found` replaces the
 * form with its message.
 */
export function SettingsView({ me }: { me: { id: string; isAdmin: boolean } }) {
  const q = useQuery({
    queryKey: queryKeys.settings,
    queryFn: () => apiFetch<Settings>("/api/settings"),
  });

  if (q.isPending) return <p className="mt-4">Loading…</p>;
  if (!q.data) {
    const notSetUp = q.error instanceof ApiError && q.error.code === "company_settings_not_found";
    return notSetUp ? (
      <p role="alert" className="mt-4">
        {q.error.message}
      </p>
    ) : (
      <ErrorBanner error={q.error} onRetry={() => void q.refetch()} />
    );
  }
  const s = q.data;
  return (
    <>
      <p className="mt-2 text-sm text-muted">
        Last changed {new Date(s.updated_at).toLocaleString()} by {who(s.updated_by, me.id)}.
      </p>
      {me.isAdmin ? (
        <SettingsForm settings={s} />
      ) : (
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-1">
          <dt className="text-muted">Company name</dt>
          <dd>{s.name}</dd>
          <dt className="text-muted">Tax rate</dt>
          <dd>{formatBps(s.tax_rate_bps)}</dd>
          <dt className="text-muted">Retirement rate</dt>
          <dd>{formatBps(s.retirement_bps)}</dd>
          <dt className="text-muted">Time zone</dt>
          <dd>{s.timezone}</dd>
          <dt className="text-muted">Biweekly anchor</dt>
          <dd>{s.biweekly_anchor}</dd>
          <dt className="text-muted">Dual approval</dt>
          <dd>{s.require_dual_approval ? "Required" : "Off"}</dd>
        </dl>
      )}
    </>
  );
}

export function SettingsForm({ settings }: { settings: Settings }) {
  const qc = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const [v, setV] = useState<Values>(() => toValues(settings));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [confirmOff, setConfirmOff] = useState(false);
  const [saved, setSaved] = useState(false);
  const zones = useMemo(() => {
    const all = timeZones();
    return all.includes(v.timezone) ? all : [v.timezone, ...all];
  }, [v.timezone]);

  const save = useMutation({
    mutationFn: (body: SettingsBody) =>
      apiFetch<Settings>("/api/settings", { method: "PUT", body }),
    retry: 0,
    onSuccess: (s) => {
      qc.setQueryData(queryKeys.settings, s);
      setSaved(true);
    },
    onError: (err) => {
      const fe =
        err instanceof ApiError && err.code === "anchor_locked"
          ? { biweekly_anchor: err.message }
          : apiFieldErrors(err);
      setErrors(fe);
      setTimeout(() => focusFirstInvalid(formRef.current), 0);
    },
  });

  function set<K extends keyof Values>(k: K, value: Values[K]) {
    setSaved(false);
    setV((old) => ({ ...old, [k]: value }));
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const fe: FieldErrors = {};
    const tax = parsePercent(v.tax);
    const ret = parsePercent(v.retirement);
    if (tax === null) fe.tax_rate_bps = "Enter a percentage like 20 or 7.65.";
    if (ret === null) fe.retirement_bps = "Enter a percentage like 5 or 2.5.";
    const parsed = SettingsBody.safeParse({
      name: v.name,
      tax_rate_bps: tax ?? -1,
      retirement_bps: ret ?? -1,
      timezone: v.timezone,
      biweekly_anchor: v.biweekly_anchor,
      require_dual_approval: v.require_dual_approval,
    });
    const all = {
      ...(parsed.success
        ? {}
        : zodFieldErrors(parsed.error, {
            tax_rate_bps: "The tax rate must be between 0% and 50%.",
            retirement_bps: "The retirement rate must be between 0% and 20%.",
          })),
      ...fe,
    };
    setErrors(all);
    if (!parsed.success || Object.keys(all).length > 0) {
      setTimeout(() => focusFirstInvalid(formRef.current), 0);
      return;
    }
    save.mutate(parsed.data);
  }

  const err = save.error;
  return (
    <form ref={formRef} className="mt-4 flex max-w-md flex-col gap-4" onSubmit={submit} noValidate>
      <TextField
        label="Company name"
        name="name"
        value={v.name}
        onChange={(x) => set("name", x)}
        error={errors.name}
      />
      <TextField
        label="Tax rate (%)"
        name="tax_rate_bps"
        inputMode="decimal"
        value={v.tax}
        onChange={(x) => set("tax", x)}
        error={errors.tax_rate_bps}
        hint="0 to 50, up to 2 decimals."
      />
      <TextField
        label="Retirement rate (%)"
        name="retirement_bps"
        inputMode="decimal"
        value={v.retirement}
        onChange={(x) => set("retirement", x)}
        error={errors.retirement_bps}
        hint="0 to 20, up to 2 decimals."
      />
      <SelectField
        label="Time zone"
        name="timezone"
        value={v.timezone}
        onChange={(x) => set("timezone", x)}
        error={errors.timezone}
        options={zones.map((z) => ({ value: z, label: z }))}
      />
      <TextField
        label="Biweekly anchor"
        name="biweekly_anchor"
        type="date"
        value={v.biweekly_anchor}
        onChange={(x) => set("biweekly_anchor", x)}
        error={errors.biweekly_anchor}
        hint="A Monday. Biweekly periods start every 14 days from it."
      />
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          role="switch"
          name="require_dual_approval"
          checked={v.require_dual_approval}
          aria-checked={v.require_dual_approval}
          onChange={(e) => {
            if (!e.target.checked && settings.require_dual_approval) setConfirmOff(true);
            else set("require_dual_approval", e.target.checked);
          }}
        />
        <span>Require dual approval</span>
      </label>
      <FormError
        message={
          err && !(err instanceof ApiError && err.code === "anchor_locked") ? errorText(err) : null
        }
        requestId={err instanceof ApiError ? err.requestId : null}
      />
      {saved ? <p role="status">Settings saved.</p> : null}
      <button
        type="submit"
        className="w-fit rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
        disabled={save.isPending}
      >
        Save settings
      </button>
      {confirmOff ? (
        <ConfirmDialog
          title="Turn off dual approval?"
          confirmLabel="Turn off"
          onClose={() => setConfirmOff(false)}
          onConfirm={() => {
            set("require_dual_approval", false);
            setConfirmOff(false);
          }}
        >
          <p>Runs already calculated keep dual approval.</p>
        </ConfirmDialog>
      ) : null}
    </form>
  );
}
