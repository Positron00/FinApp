import { toxiproxyUrl } from "./env";

/**
 * Toxiproxy (platform LLD §7.4; web LLD §9.2): the test stack routes web → payroll and
 * worker → bank-stub through the `payroll` and `bank-stub` proxies. Disabling one closes its
 * listener, so callers get a connection error at once. Specs call `resetProxies()` in
 * `afterEach`/`afterAll`, which re-enables every proxy and removes every toxic.
 */
export type ProxyName = "payroll" | "bank-stub" | "postgres" | "redis";

export async function setProxyEnabled(name: ProxyName, enabled: boolean): Promise<void> {
  const res = await fetch(`${toxiproxyUrl()}/proxies/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error(`toxiproxy ${name} enabled=${enabled}: HTTP ${res.status}`);
}

export async function resetProxies(): Promise<void> {
  const res = await fetch(`${toxiproxyUrl()}/reset`, { method: "POST" });
  if (!res.ok) throw new Error(`toxiproxy reset: HTTP ${res.status}`);
}
