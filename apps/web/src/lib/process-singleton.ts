/**
 * Process-wide state. Next.js bundles instrumentation, route handlers and pages separately, so
 * a module-level `let` would give each bundle its own copy (three Redis clients, three
 * breakers per target). Keeping the value on `globalThis` makes it one per process.
 */
export function processSingleton<T>(key: string, make: () => T): T {
  const g = globalThis as unknown as Record<symbol, T | undefined>;
  const sym = Symbol.for(`payrun.web.${key}`);
  return (g[sym] ??= make());
}

/** Tests only: replace or clear a process singleton. */
export function setProcessSingleton<T>(key: string, value: T | undefined): void {
  const g = globalThis as unknown as Record<symbol, T | undefined>;
  g[Symbol.for(`payrun.web.${key}`)] = value;
}
