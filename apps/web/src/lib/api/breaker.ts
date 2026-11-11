import "server-only";

import { config } from "../config";
import { logger } from "../logger";
import { metricsApi } from "../metrics";
import { processSingleton } from "../process-singleton";

/**
 * A per-process circuit breaker per target (web LLD §3.4.2): closed → open after
 * BREAKER_FAILURES consecutive failures; open → half-open after BREAKER_RESET_S; in half-open one
 * probe runs and the rest fail fast; the probe's success closes it, its failure re-opens it.
 */
export type BreakerState = "closed" | "open" | "half_open";
const GAUGE: Record<BreakerState, 0 | 1 | 2> = { closed: 0, half_open: 1, open: 2 };

export class Breaker {
  private state: BreakerState = "closed";
  private failures = 0;
  private openedAt = 0;
  private probing = false;

  constructor(readonly target: string) {}

  get current(): BreakerState {
    return this.state;
  }

  /** "allow" (closed), "probe" (the one half-open request) or "reject" (fail fast). */
  acquire(now: number = Date.now()): "allow" | "probe" | "reject" {
    if (this.state === "closed") return "allow";
    if (this.state === "open") {
      if (now - this.openedAt < config().BREAKER_RESET_S * 1000) return "reject";
      this.transition("half_open");
    }
    if (this.probing) return "reject";
    this.probing = true;
    return "probe";
  }

  onSuccess(): void {
    this.failures = 0;
    this.probing = false;
    if (this.state !== "closed") this.transition("closed");
  }

  onFailure(now: number = Date.now()): void {
    this.probing = false;
    if (this.state === "half_open") {
      this.open(now);
      return;
    }
    this.failures += 1;
    if (this.state === "closed" && this.failures >= config().BREAKER_FAILURES) this.open(now);
  }

  private open(now: number): void {
    this.openedAt = now;
    this.failures = 0;
    this.transition("open");
  }

  private transition(to: BreakerState): void {
    if (this.state === to) return;
    this.state = to;
    metricsApi.breakerState(this.target, GAUGE[to]);
    if (to === "open") {
      logger().warn({ event: "external.breaker.opened", target: this.target }, "breaker opened");
    } else if (to === "closed") {
      logger().info({ event: "external.breaker.closed", target: this.target }, "breaker closed");
    }
  }
}

const breakers = () => processSingleton("breakers", () => new Map<string, Breaker>());

export function breakerFor(target: string): Breaker {
  let b = breakers().get(target);
  if (!b) {
    b = new Breaker(target);
    breakers().set(target, b);
  }
  return b;
}

/** Tests only. */
export function resetBreakersForTests(): void {
  breakers().clear();
}
