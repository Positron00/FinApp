import { Breaker, breakerFor } from "@/lib/api/breaker";
import { resilientFetch } from "@/lib/api/resilient-fetch";

import { PAYROLL } from "../../helpers/env";
import { t } from "../../helpers/state";

/** plan §4, §2.2: the per-target circuit breaker. */
describe("Breaker", () => {
  // plan §4: opens after BREAKER_FAILURES (5) consecutive failures.
  it("opens after 5 consecutive failures", () => {
    const b = new Breaker("payroll");
    for (let i = 0; i < 4; i++) b.onFailure(0);
    expect(b.current).toBe("closed");
    b.onFailure(0);
    expect(b.current).toBe("open");
    expect(b.acquire(1000)).toBe("reject");
    expect(t.logs.events("external.breaker.opened")[0]).toMatchObject({ target: "payroll" });
  });

  // plan §4: a success resets the consecutive count.
  it("resets the count on success", () => {
    const b = new Breaker("payroll");
    for (let i = 0; i < 4; i++) b.onFailure(0);
    b.onSuccess();
    for (let i = 0; i < 4; i++) b.onFailure(0);
    expect(b.current).toBe("closed");
  });

  // plan §4: half-open after BREAKER_RESET_S (30 s) lets one probe through.
  it("half-opens after 30 s with a single probe; success closes", () => {
    const b = new Breaker("identity");
    for (let i = 0; i < 5; i++) b.onFailure(0);
    expect(b.acquire(29_999)).toBe("reject");
    expect(b.acquire(30_000)).toBe("probe");
    expect(b.current).toBe("half_open");
    expect(b.acquire(30_001)).toBe("reject");
    b.onSuccess();
    expect(b.current).toBe("closed");
    expect(b.acquire(30_002)).toBe("allow");
    expect(t.logs.events("external.breaker.closed")).toHaveLength(1);
  });

  // plan §4: a failed probe re-opens.
  it("re-opens when the probe fails", () => {
    const b = new Breaker("identity");
    for (let i = 0; i < 5; i++) b.onFailure(0);
    expect(b.acquire(30_000)).toBe("probe");
    b.onFailure(30_000);
    expect(b.current).toBe("open");
    expect(b.acquire(59_999)).toBe("reject");
  });

  // plan §4: a probe the caller aborts gives no verdict but frees the probe slot, so the
  // breaker can't stick in half-open rejecting everything.
  it("frees the half-open probe when the caller aborts it", async () => {
    const b = breakerFor("payroll");
    for (let i = 0; i < 5; i++) b.onFailure(Date.now() - 31_000);
    const ac = new AbortController();
    const f = resilientFetch("payroll", {
      fetchImpl: (_r, init) =>
        new Promise((_, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    });
    const pending = f(new Request(`${PAYROLL}/v1/x`, { signal: ac.signal }));
    ac.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(b.current).toBe("half_open");
    expect(b.acquire()).toBe("probe");
  });

  // plan §4: while open, calls fail fast with 503 and no network call; 4xx and 500 don't count.
  it("fails fast without fetching while open, and ignores 4xx and 500", async () => {
    let calls = 0;
    const respond = (code: number) => async () => {
      calls++;
      return new Response("{}", { status: code });
    };
    const req = () => new Request(`${PAYROLL}/v1/x`, { method: "POST", body: "{}" });
    for (let i = 0; i < 6; i++)
      await resilientFetch("payroll", { fetchImpl: respond(i % 2 ? 404 : 500) })(req());
    expect(breakerFor("payroll").current).toBe("closed");
    for (let i = 0; i < 5; i++) await resilientFetch("payroll", { fetchImpl: respond(502) })(req());
    expect(breakerFor("payroll").current).toBe("open");
    calls = 0;
    await expect(
      resilientFetch("payroll", { fetchImpl: respond(200) })(req()),
    ).rejects.toMatchObject({ status: 503 });
    expect(calls).toBe(0);
    expect(t.logs.events("bff.upstream_error").at(-1)).toMatchObject({
      error_kind: "breaker_open",
    });
  });
});
