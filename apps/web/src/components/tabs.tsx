"use client";

import type { KeyboardEvent, ReactNode } from "react";

/**
 * The WAI-ARIA tabs pattern (web LLD §6.3, PAY-NFR-14), without Radix: a roving `tabIndex` (only
 * the selected tab is in the tab order) and automatic activation with ArrowLeft/ArrowRight/
 * Home/End. Each tab controls `panel-<id>`; render panels with `<TabPanel>`.
 */
export function TabList<T extends string>({
  tabs,
  active,
  onSelect,
  label,
}: {
  tabs: readonly { id: T; label: string }[];
  active: T;
  onSelect: (t: T) => void;
  label: string;
}) {
  const ids = tabs.map((t) => t.id);
  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    const i = ids.indexOf(active);
    const next =
      e.key === "ArrowRight"
        ? ids[(i + 1) % ids.length]
        : e.key === "ArrowLeft"
          ? ids[(i - 1 + ids.length) % ids.length]
          : e.key === "Home"
            ? ids[0]
            : e.key === "End"
              ? ids[ids.length - 1]
              : undefined;
    if (next === undefined) return;
    e.preventDefault();
    onSelect(next);
    document.getElementById(`tab-${next}`)?.focus();
  }
  return (
    <div role="tablist" aria-label={label} className="mt-6 flex gap-2 border-b">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          id={`tab-${t.id}`}
          aria-selected={active === t.id}
          aria-controls={`panel-${t.id}`}
          tabIndex={active === t.id ? 0 : -1}
          className={`px-3 py-2 ${active === t.id ? "border-b-2 border-brand font-semibold" : ""}`}
          onClick={() => onSelect(t.id)}
          onKeyDown={onKeyDown}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

/** One tab panel; the inactive ones are `hidden` and their content unmounted. */
export function TabPanel({
  id,
  active,
  children,
}: {
  id: string;
  active: boolean;
  children: ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      id={`panel-${id}`}
      aria-labelledby={`tab-${id}`}
      hidden={!active}
      tabIndex={0}
    >
      {active ? children : null}
    </div>
  );
}
