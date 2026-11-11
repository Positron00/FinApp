import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll } from "vitest";

/**
 * MSW for identity and payroll at their test base URLs (web LLD §9.1). Unit tests have no
 * network: an unhandled request fails the test. `seen` records every intercepted request.
 */
export const server = setupServer();
export const seen: Request[] = [];

export function setupMsw(): void {
  beforeAll(() => {
    server.listen({ onUnhandledFrame: "error" });
    server.events.on("request:start", ({ request }) => {
      seen.push(request.clone());
    });
  });
  afterEach(() => {
    server.resetHandlers();
    seen.length = 0;
  });
  afterAll(() => server.close());
}
