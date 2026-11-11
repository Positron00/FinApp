# Low-level design: web (Next.js pages and BFF)

Source: [architecture plan](../../plan/architecture-plan.md) v1.2 (accepted 2026-11-10), [ADR 0002](../../adr/0002-sign-in-and-roles.md), [ADR 0005](../../adr/0005-four-eyes-approval.md), [ADR 0009](../../adr/0009-csp.md). Requirements: [requirements.md](requirements.md). High-level design: [design-high-level.md](design-high-level.md).

Scope: `apps/web`, the only public entry point. It serves the pages and the BFF (`/api/*`), holds browser sessions in Redis (`web:*`), mints internal JWTs and calls identity and payroll through the clients generated into `packages/api-client`. Business rules stay in the services; the BFF does sessions, authorization at the edge, validation, request shaping and error mapping.

**Stack decisions for this area:**
- **Next.js 16 (App Router), strict TypeScript.** The request interceptor is `src/proxy.ts`. Next 16's proxy runs on the Node.js runtime, but it stays a cookie-presence check for pages plus the CSP nonce and is **not** a security boundary. It never gates `/api/*` (C63) (`architecture.md` §9; CVE-2025-29927). Every route handler and every page calls `requireSession()` itself (PAY-601). No reason was found to stay on 15.
- Tailwind with shadcn/ui (Radix primitives); TanStack Query v5 in the browser; Zod at every boundary.
- **Service clients:** `openapi-typescript` types plus `openapi-fetch`, from `packages/api-client` (`@payrun/api-client`, exports `IdentityPaths` and `PayrollPaths`).
- **Server libraries:** `ioredis` for Redis, `jose` for EdDSA, `pino` for logs (bridged to the OTel logs SDK, C62), `@vercel/otel` for traces, metrics and logs.
- **Pages render dynamically.** The root layout reads the nonce header, so there's no static export (ADR 0009).
- **Server components don't call services for page data.** Each page's server component only gates (session and role), then renders client components. Those components fetch through `/api/*` with TanStack Query, so every data path goes through the same route wrapper, error mapping and metrics.
- **`next.config.ts`:**
  - `serverExternalPackages: ["pino", "ioredis"]` (C62: no `pino-opentelemetry-transport`);
  - `poweredByHeader: false`;
  - `output: "standalone"`;
  - `headers()` sets the static security headers (§5.6); HSTS depends on the runtime `ENV`, so `proxy.ts` sets it (C62).

## 1. Modules

All paths are under `apps/web/`. Everything is new.

### 1.1 Routes and pages (`src/app/`)

| Module (path) | Responsibility |
|---|---|
| `src/app/layout.tsx` | Root layout. Reads `x-nonce` with `await headers()`, sets `<html lang="en">`, renders `<Providers>` (QueryClient) and `<WebVitals>`. Reading headers makes every page dynamic |
| `src/app/page.tsx` | Redirects `/` to `/dashboard` |
| `src/app/not-found.tsx`, `src/app/error.tsx`, `src/app/global-error.tsx` | 404 page; error boundary showing the mapped message and request ID, never a stack trace |
| `src/app/(public)/login/page.tsx` | Sign-in form (§6.2) |
| `src/app/(public)/accept/page.tsx` | Accept an invite: reads `#token`, sets a password (PAY-103) |
| `src/app/(public)/reset/page.tsx` | Request a reset (no fragment), or confirm one (`#token`) (PAY-105) |
| `src/app/(app)/layout.tsx` | `requireSessionForPage()`, then renders `<AppShell>` with role-gated navigation |
| `src/app/(app)/dashboard/page.tsx` | Dashboard |
| `src/app/(app)/employees/page.tsx`, `employees/new/page.tsx`, `employees/[id]/page.tsx` | List; create (payroll_admin); view and edit (edit: payroll_admin) |
| `src/app/(app)/settings/page.tsx` | Company settings (view: any role; edit: admin) |
| `src/app/(app)/users/page.tsx` | Users and invites (admin) |
| `src/app/(app)/pay-runs/page.tsx`, `pay-runs/new/page.tsx`, `pay-runs/[id]/page.tsx` | Run list; create (payroll_admin); run detail with the `payslips` and `payouts` tabs (`?tab=`) |
| `src/app/(app)/audit/page.tsx` | Auth audit and payroll audit tabs (admin) |

Every `page.tsx` under `(app)` calls `requireSessionForPage(path, roles?)` itself (tested by `tests/app/page-gates.test.tsx`). The layout's check isn't enough, because layouts aren't re-rendered on client navigation.

### 1.2 BFF route handlers (`src/app/api/`)

There's one `route.ts` per path. Each exported method is built with `route()` (§3.1); the full table is §3.2.

| File | Methods |
|---|---|
| `api/auth/login/route.ts` | POST |
| `api/auth/logout/route.ts` | POST |
| `api/auth/accept/route.ts` | POST |
| `api/auth/reset/request/route.ts` | POST |
| `api/auth/reset/confirm/route.ts` | POST |
| `api/auth/me/route.ts` | GET |
| `api/users/route.ts` | GET |
| `api/users/invite/route.ts` | POST |
| `api/users/[id]/route.ts` | PATCH |
| `api/invites/[id]/resend/route.ts` | POST |
| `api/settings/route.ts` | GET, PUT |
| `api/employees/route.ts` | GET, POST |
| `api/employees/[id]/route.ts` | GET, PATCH |
| `api/pay-runs/route.ts` | GET, POST |
| `api/pay-runs/[id]/route.ts` | GET, DELETE |
| `api/pay-runs/[id]/hours/route.ts` | PUT |
| `api/pay-runs/[id]/calculate/route.ts` | POST |
| `api/pay-runs/[id]/approve/route.ts` | POST |
| `api/pay-runs/[id]/payslips/route.ts` | GET |
| `api/pay-runs/[id]/payslips/[payslipId]/route.ts` | GET |
| `api/pay-runs/[id]/payouts/route.ts` | GET |
| `api/pay-runs/[id]/payouts/[payoutId]/retry/route.ts` | POST |
| `api/pay-runs/[id]/payouts/[payoutId]/resolve/route.ts` | POST (PAY-502) |
| `api/audit/auth/route.ts` | GET |
| `api/audit/payroll/route.ts` | GET |
| `api/telemetry/route.ts` | POST |
| `api/livez/route.ts`, `api/readyz/route.ts` | GET (liveness: no dependencies; readiness: Redis `PING` within `REDIS_SOCKET_TIMEOUT_MS`). These are the web's only health routes (C4): the Compose healthcheck and the Kubernetes readiness probe use `http://127.0.0.1:3000/api/readyz`, the liveness probe uses `/api/livez`, and `seed.py` waits on `/api/readyz` |
| `api/[...path]/route.ts` | Every method: 404 `not_found` as JSON, through `route()` so the standard headers are set (C15) |

Every route file also exports the standard methods it doesn't implement (GET, POST, PUT, PATCH, DELETE) as `methodNotAllowed(allowed)` from `lib/route.ts`. That handler returns 405 `method_not_allowed` with an `Allow` header and the standard headers, and records the request metric; it runs no session, limiter or body step.

### 1.3 Server libraries (`src/lib/`)

Every module here starts with `import "server-only"`, except the ones marked *shared*.

| Module | Responsibility |
|---|---|
| `lib/config.ts` | Parses `process.env` with Zod into a frozen `Config` (§8) and runs the web startup checks. Loaded lazily, so `next build` runs without secrets |
| `lib/redis.ts` | The `ioredis` singleton built from `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME` (`web`) and `REDIS_PASSWORD` (C12): `commandTimeout = REDIS_SOCKET_TIMEOUT_MS`, `connectTimeout = REDIS_SOCKET_TIMEOUT_MS`, `enableOfflineQueue: false`, `maxRetriesPerRequest: 1`, `enableReadyCheck: false` (the `web` ACL denies `INFO`, which the ready check runs; C62), connecting at server start (`lazyConnect: false`; `lib/otel.ts` calls `redis()`). With these settings a Redis outage fails fast instead of queueing. The client lives on `globalThis` (`lib/process-singleton.ts`), because Next bundles instrumentation, route handlers and pages separately (C62) |
| `lib/session.ts` | The session store: `createSession`, `loadSession`, `touchSession`, `markRolesChecked`, `deleteSession`, `revokeUserSessions`, plus the cookie helpers (§4) |
| `lib/require-session.ts` | `resolveSession(cookie, {roles, ip})` (used by `route()`), `requireSession(roles?)` (reads `cookies()` and `headers()`) and `requireSessionForPage(path, roles?)` for pages (§4.3) |
| `lib/internal-jwt.ts` | `internalToken()`: EdDSA minting (§3.4.1) |
| `lib/api/resilient-fetch.ts` | `resilientFetch(target, opts)`: timeouts, GET retries, breaker, metrics, `X-Request-ID` (§3.4.2) |
| `lib/api/breaker.ts` | A per-process circuit breaker per target |
| `lib/api/clients.ts` | `identityClient(ctx)` and `payrollClient(ctx)`: `openapi-fetch` clients over `IdentityPaths` / `PayrollPaths`, with a middleware that adds `Authorization: Bearer <jwt aud=identity\|payroll>` and `X-Request-ID`; the identity client also adds `X-Client-IP` when the client IP is known. When `clientIp()` returns `null` the header is **omitted**, and identity puts the request in its shared `unknown` per-IP bucket (R18, C16). `slowPayrollFetch(ctx)` is the calculate/approve fetch (§3.4.2) |
| `lib/api/call.ts` | `callService(target, route, call, schema)`: maps a non-2xx through the catalogue (§3.3) and parses a 2xx with its §2.4 schema |
| `lib/route.ts` | The `route()` wrapper: the request pipeline (§3.1); `methodNotAllowed(allowed)` (§1.2) |
| `lib/rate-limit.ts` | Sliding-window limiter on Redis (§5.1) |
| `lib/client-ip.ts` | `clientIp(headers, hops)` from `X-Forwarded-For` and `TRUSTED_PROXY_HOPS`; `null` when unknown (§5.2) |
| `lib/origin.ts` | `checkOrigin(req)` for mutations (§5.4) |
| `lib/body.ts` | `readJson(req, schema, limit)`: a size-capped body read plus Zod parsing (§5.3) |
| `lib/errors.ts` | `BffError`, the error catalog and `mapUpstreamError()` (§3.3); `toErrorResponse()` |
| `lib/logger.ts` | The pino root logger: trace mixin, redactor (§7.1) |
| `lib/metrics.ts` | OTel meter `web`: instruments (§7.2) |
| `lib/csp.ts` | `buildCsp(nonce, env)` and `newNonce()` (§5.5) |
| `lib/request-context.ts` | `AsyncLocalStorage<{requestId}>` so the clients and the logger see the request ID |
| `lib/process-singleton.ts` | Process-wide singletons on `globalThis` (the Redis client, breakers, the re-check single-flight map, the request context; C62) |
| `lib/redact.ts` (*shared*) | The deep key redactor used by the logger (§7.1) |
| `lib/otel.ts` | `startNode()`: config check, `registerOTel` (§7.4) |
| `lib/schemas/*.ts` (*shared*) | Zod request and response schemas (§2.3, §2.4) |
| `lib/money.ts` (*shared*) | Converts between strings and cents, percent and basis points, hours and hundredths, using integer string arithmetic with no `parseFloat` (ADR 0006); `formatCents()` |
| `lib/aba.ts` (*shared*) | ABA routing checksum, for instant form feedback (payroll is authoritative) |
| `lib/roles.ts` (*shared*) | `Role` type and `hasAnyRole(roles, required)` |
| `lib/payout-badge.ts` (*shared*) | Pure function: payout and current attempt → badge (§6.3) |
| `lib/client/api.ts` (*shared, browser*) | `apiFetch()`: same-origin fetch to `/api/*`. It parses the `{error}` body into an `ApiError {status, code, message, requestId, details}` and redirects to `/login?next=` on 401 |
| `lib/client/query-keys.ts` (*shared*) | TanStack Query keys |

### 1.4 Entry points and components

| Module | Responsibility |
|---|---|
| `src/instrumentation.ts` | `register()`: on `nodejs` only, runs `loadConfig()` (on a failed check: logs `config.invalid` and exits 2; C29), then `registerOTel` (§7.4). `onRequestError` logs `web.server_error` |
| `src/proxy.ts` | Cookie-presence gate, CSP nonce, `x-request-id` (§5.5) |
| `src/components/ui/*` | shadcn/ui primitives (Button, Dialog, AlertDialog, Table, Tabs, Badge, Form, Input, Select, Checkbox, Toast, Skeleton). **As built (F5):** no shadcn/Radix or react-hook-form dependency was added (the lock file is the lead's). The same contracts come from small local components: `dialog.tsx` (`Dialog`/`ConfirmDialog`: `role="dialog"`/`"alertdialog"`, `aria-modal`, initial focus, Tab trap, Escape, focus returned to the opener), `tabs.tsx` (`TabList`/`TabPanel`, the WAI-ARIA pattern of §6.3), `field.tsx` (`TextField`/`SelectField`/`FormError`: `<label htmlFor>` with the hint and error outside the label, linked by `aria-describedby`). Toasts are inline `role="status"` notices |
| `src/components/app-shell.tsx`, `nav.tsx` | Layout, skip link, nav items filtered by role, user menu with sign-out |
| `src/components/role-gate.tsx` | Shows children only for the given roles. This is a UI convenience; the server enforces |
| `src/components/providers.tsx` | QueryClientProvider (defaults in §6.1) and the Toaster |
| `src/components/web-vitals.tsx` | `useReportWebVitals` → `navigator.sendBeacon("/api/telemetry")`; a `window.onerror` / `unhandledrejection` reporter |
| `src/components/money.tsx`, `data-table.tsx`, `cursor-pager.tsx`, `error-banner.tsx`, `confirm-dialog.tsx`, `live-region.tsx`, `forbidden.tsx` | Shared UI |
| `src/components/forms/{login-form,accept-form,reset-forms,password-set-form,employee-form,settings-form,run-create-form}.tsx`, `src/components/users/users-page.tsx` (with the invite and user-edit dialogs), `src/components/employees/{employee-list,employee-detail,bank-fields}.tsx`, `src/components/dashboard.tsx` | Forms with the shared Zod schemas (controlled React state, no react-hook-form). `lib/client/form-errors.ts` maps 422 `details` (C56 `body.<field>`) to inline field errors and moves focus to the first invalid field; `lib/client/fragment-token.ts` reads and strips `#token` |
| `src/components/runs/{run-detail,run-status-badge,hours-editor,payslip-dialog,payouts-tab,approve-button,retry-button,resolve-dialog}.tsx` | The run page (§6.3). As built, the header, actions and payslips tab live in `run-detail.tsx`, and the attempt history in `payouts-tab.tsx` |
| `src/components/audit/{auth-audit-table,payroll-audit-table}.tsx` | Audit tabs |

## 2. Data models

### 2.1 Redis keys (ACL user `web`, pattern `~web:*`)

| Key | Type | Fields / members | TTL | Requirement |
|---|---|---|---|---|
| `web:sess:<k>` | hash | `user_id` (uuid), `cid` (uuid), `name` (string, the display name from `SessionUser`, refreshed on each `/me` re-check; C10), `roles` (JSON array of `admin`/`payroll_admin`/`approver`), `roles_version` (int), `created_at`, `last_seen`, `roles_checked_at` (epoch ms, int) | `min(SESSION_IDLE_TTL_S, created_at + SESSION_ABS_TTL_S − now)`, refreshed by the touch (§4.2) | PAY-106 |
| `web:user-sess:<user_id>` | set | the `<k>` of each live session of that user | `SESSION_ABS_TTL_S`, reset on every `SADD` | PAY-106, 107 |
| `web:rl:<scope>:<subject>:<window>` | string (counter) | `scope` ∈ `ip`, `session`, `telemetry`; `subject` = 16 hex chars of `HMAC-SHA256(SESSION_SECRET, ip or k)`; `window` = `floor(now_s / 60)` | 120 s | PAY-603 |

- `<k>` = `HMAC-SHA256(SESSION_SECRET, <session id>)` as 64 hex chars. The cookie holds the raw ID (256 random bits, base64url, 43 chars). Redis holds only the HMAC, so a Redis dump or `MONITOR` output yields no usable cookie (R23, PAY-106).
- **The JWT `sid` claim** is the first 32 hex chars of `<k>`. It's stable per session, can't be turned back into the cookie, and lets service logs be correlated with a session.
- **ACL.** The ACL user `web` is defined by the platform design and provisioned in the deploy bootstrap; its password is the `.env`/secret value `REDIS_WEB_PASSWORD`, which the web container receives as `REDIS_PASSWORD` (C12). Its key pattern is `~web:*`, with no stream, scripting, pub/sub or admin commands. The web uses only these commands: `GET SET DEL EXISTS INCR EXPIRE PEXPIRE PTTL HSET HGET HGETALL SADD SREM SMEMBERS MULTI EXEC PING`. The ACL denies `INFO`, so the ioredis ready check is off (`enableReadyCheck: false`; C62). So web can't touch `stream:*`, `identity:*` or `payroll:*`, and can't `XADD` (PAY-607).

### 2.2 Session types (`lib/session.ts`)

```ts
export type Role = "admin" | "payroll_admin" | "approver";
export interface SessionRecord {            // the Redis hash, parsed (PAY-106)
  user_id: string; cid: string; name: string; roles: Role[]; roles_version: number;
  created_at: number; last_seen: number; roles_checked_at: number;
}
export interface Session extends SessionRecord {
  key: string;      // <k>, the HMAC of the cookie value
  sid: string;      // key.slice(0, 32), the JWT sid claim
}
```

**Cookie:**
- Name `SESSION_COOKIE_NAME`: `sid` in dev, `__Host-sid` in uat and prod.
- Attributes: `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=SESSION_ABS_TTL_S`, with no `Domain`.
- Browsers treat `127.0.0.1` as a secure context, so `Secure` works over plain http in dev and e2e. The dev origin is `http://127.0.0.1:${WEB_PORT}` (C12); every test and browser uses `127.0.0.1`, never `localhost`, because the cookie and the Origin check (§5.4) are per host.

### 2.3 Request schemas (`lib/schemas/`, shared with the forms)

These are common to all of them:
- `Email = z.string().trim().toLowerCase().email().max(254)`
- `Password = z.string().min(12).max(256)` (12 is PAY-103; the BFF checks it early so the user gets instant feedback, and identity stays authoritative)
- `Uuid = z.string().uuid()`
- `IsoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/)`
- `Cursor = z.string().max(512).regex(/^[A-Za-z0-9_-]+$/).optional()`: opaque base64url, passed through unchanged (R4)
- `Limit = z.coerce.number().int().min(1).max(100).default(50)`

Every object schema is `.strict()`: an unknown key gives 422 `validation_error`. Validation `details` are always `[{field, issue}]` (`field` is the dotted path, `issue` the Zod issue code) and never echo the submitted value (C14).

| Schema | Shape | Requirement |
|---|---|---|
| `LoginBody` | `{email: Email, password: z.string().min(1).max(256)}` (no minimum length at sign-in) | PAY-104 |
| `AcceptBody` | `{token: z.string().regex(/^[A-Za-z0-9_-]{20,128}$/), password: Password}` | PAY-103 |
| `ResetRequestBody` | `{email: Email}` | PAY-105 |
| `ResetConfirmBody` | `{token: <as AcceptBody>, password: Password}` | PAY-105 |
| `ListQuery` | `{cursor: Cursor, limit: Limit}`. Used by every list route; the BFF passes both to the service unchanged (C8) | PAY-108, 109, 202, 303, 501, 602 |
| `InviteBody` | `{email: Email, name: z.string().trim().min(1).max(200), roles: z.array(RoleEnum).min(1).max(3)}`, with roles required to be unique | PAY-101 |
| `UserPatchBody` | `{roles?: RoleEnum[] (min 1, max 3, unique), status?: z.literal("inactive")}`; at least one field. There is no user reactivation in 0.1.0 (R37, C25), so `"active"` is a 422 | PAY-108 |
| `SettingsBody` | `{name: 1–200, tax_rate_bps: int 0–5000, retirement_bps: int 0–2000, timezone: one of Intl.supportedValuesOf("timeZone"), biweekly_anchor: IsoDate that is a Monday, require_dual_approval: boolean}` | PAY-200 |
| `EmployeeCreateBody` | A discriminated union on `pay_type`. Common fields: `name` 1–200, `email: Email`, `schedule: "biweekly"\|"monthly"`, `bank_routing: /^\d{9}$/` with the ABA checksum, `bank_account: /^\d{4,17}$/`. Then `salaried` with `annual_salary_cents: int 1–1_000_000_000`, or `hourly` with `hourly_rate_cents: int 1–100_000` | PAY-201 |
| `EmployeePatchBody` | Every field of `EmployeeCreateBody` optional, plus `status?: "active"\|"inactive"`; at least one field. Employee reactivation stays in scope (PAY-203); reactivating an employee whose bank details were purged needs `bank_routing` and `bank_account` in the same PATCH (payroll returns 422 otherwise) | PAY-203 |
| `EmployeeListQuery` | `ListQuery & {status?: "active"\|"inactive"}` | PAY-202 |
| `PayRunListQuery` | `ListQuery & {status?: "draft"\|"calculated"\|"processing"\|"paid"\|"partially_failed"}` | PAY-303 |
| `PayoutListQuery` | `ListQuery & {status?: "requested"\|"paid"\|"failed"}` | PAY-501 |
| `PayRunCreateBody` | `{schedule: "biweekly"\|"monthly", period_start: IsoDate}` | PAY-300 |
| `HoursBody` | `{entries: z.array({employee_id: Uuid, hours_hundredths: int 0–74_400}).min(1).max(500)}`, with unique `employee_id`. The per-schedule bound (33,600 biweekly) is payroll's (PAY-301) | PAY-301 |
| `IdempotencyKeyHeader` | `Uuid`. The browser generates it with `crypto.randomUUID()`. A **missing** header gives **400 `idempotency_key_required`**; a present but malformed one gives 422 `validation_error` with `field = "Idempotency-Key"` (C34) | PAY-400 |
| `ResolveBody` | `{action: z.enum(["redrive","mark_rejected"]), note: z.string().trim().min(1).max(500)}` | PAY-502 |
| `TelemetryBody` | A discriminated union. `{type: "vital", name: "CLS"\|"FCP"\|"INP"\|"LCP"\|"TTFB", value: number ≥ 0, rating: "good"\|"needs-improvement"\|"poor", route: string ≤ 100}`, or `{type: "error", message: string ≤ 500, route: string ≤ 100}` | §7.5 |
| `IdParam` | `{id: Uuid}`; `RunPayslipParam = {id: Uuid, payslipId: Uuid}`; `RunPayoutParam = {id: Uuid, payoutId: Uuid}` | all `[id]` routes |

Forms enter money, rates and hours as text. `lib/money.ts` converts them with string-integer parsing:
- money: `^\d{1,8}(\.\d{1,2})?$` → cents;
- percent: `^\d{1,2}(\.\d{1,2})?$` → basis points;
- hours: `^\d{1,3}(\.\d{1,2})?$` → hundredths.

There's no float anywhere on the path (ADR 0006).

### 2.4 Response schemas

The generated types in `packages/api-client` are the contract. **The Zod schemas below mirror the service Out models field for field** (C5): same names, same nullability, same enums. A type-level test (`expectTypeOf<z.infer<typeof X>>().toEqualTypeOf<...>()`) fails when they drift. The BFF parses every service response it returns, because these responses are shown to users (`architecture.md` §4).
- **Default mode is strip:** unknown keys are dropped.
- **`Employee`, `Payout` and `Attempt` are `.strict()`** (C5: strict stays, to block leaks). Any unexpected key (for example a `bank_account` or `routing` field) fails parsing. The BFF then returns 500 `internal_error` and logs `bff.response_invalid`, so a full bank number can't leak through the BFF (PAY-202, PAY-501, PAY-NFR-08).
- **Types:** `Uuid` as in §2.3; `DateTime = z.string().datetime({ offset: true })`; `Date = IsoDate`; `Cents = z.number().int()`; `Role` and `Schedule` as in §2.3; `PayType = z.enum(["salaried","hourly"])`.

| Schema | Mirrors | Fields | Requirement |
|---|---|---|---|
| `Page<T>` | payroll `Page[T]`, identity `UserListOut` / `AuditListOut` | `{items: T[], next_cursor: string \| null}` (R4, C8). Every list route returns it, payslips and payouts included | PAY-108, 109, 202, 303, 501, 602 |
| `SessionUser` | identity `SessionUser` | `{user_id: Uuid, company_id: Uuid, email: string, name: string, roles: Role[], roles_version: int, status: "active"}` (C6). Returned by identity's login and accept only; never sent to the browser | PAY-103, 104, 106 |
| `User` | identity `UserOut` | `{id: Uuid, email: string, name: string, roles: Role[], roles_version: int, status: "pending"\|"active"\|"inactive", created_at: DateTime, updated_at: DateTime, activated_at: DateTime\|null, deactivated_at: DateTime\|null, pending_invite_id: Uuid\|null}`. `pending_invite_id` is set only for `pending` users (R21, C11) | PAY-101, 107, 108 |
| `UserMe` | identity `MeOut` (`GET /v1/users/me`) | `User` fields plus `company_id: Uuid` (R19). Used by the re-check only | PAY-107 |
| `PendingInvite` | identity `PendingInviteOut` (resend response) | `{id: Uuid, expires_at: DateTime\|null, email_status: "pending"\|"sent"\|"failed"\|"cancelled"\|null}`. `expires_at` is null until the relay sends (R17, C31) | PAY-102 |
| `Me` | BFF only | `{id: Uuid, email: string, name: string, roles: Role[], roles_version: int, status: "pending"\|"active"\|"inactive"}`. Built by `toMe()`: from a `SessionUser` with `id = user_id` (`company_id` goes into the session as `cid`, not into `Me`; C6), or from a `UserMe` by picking these fields | PAY-104, 106, 107 |
| `ResetConfirmResult` | identity `ResetConfirmOut` | `{user_id: Uuid, roles_version: int}`. Used by the BFF only (R19) | PAY-105 |
| `Settings` | payroll `CompanySettingsOut` | `{name: string, tax_rate_bps: int, retirement_bps: int, timezone: string, biweekly_anchor: Date, require_dual_approval: boolean, updated_at: DateTime, updated_by: string\|null}` | PAY-200 |
| `Employee` (strict) | payroll `EmployeeOut` | `{id: Uuid, name: string, email: string, pay_type: PayType, annual_salary_cents: int\|null, hourly_rate_cents: int\|null, schedule: Schedule, account_last4: /^\d{4}$/\|null, has_bank_details: boolean, bank_updated_at: DateTime\|null, bank_updated_by: string\|null, status: "active"\|"inactive", deactivated_at: DateTime\|null, created_at: DateTime, updated_at: DateTime}`. `account_last4` is null after the bank-detail purge (C5) | PAY-201–203, 608 |
| `RunTotals` | payroll `RunTotalsOut` | `{gross_cents, tax_cents, retirement_cents, net_cents, funding_required_cents}` (all `Cents`) | PAY-303 |
| `RunCounts` | payroll `PayRunOut.counts` | `{requested, awaiting_bank, blocked, paid, no_transfer, failed}` (all int ≥ 0). `requested` includes `awaiting_bank` (current attempt `indeterminate`) and `blocked` (current attempt `needs_operator`); `paid` includes `no_transfer` (C9) | PAY-303, 413 |
| `PayRun` | payroll `PayRunOut` | `{id: Uuid, schedule: Schedule, period_start: Date, period_end: Date, pay_date: Date, period_index: int, periods_per_year: int, status: "draft"\|"calculated"\|"processing"\|"paid"\|"partially_failed", employee_count: int, tax_bps: int\|null, retirement_bps: int\|null, require_dual_approval: boolean\|null, totals: RunTotals\|null, counts: RunCounts, created_by: string, created_at: DateTime, calculated_by: string\|null, calculated_by_name: string\|null, calculated_at: DateTime\|null, approved_by: string\|null, approved_by_name: string\|null, approved_at: DateTime\|null, processing_since: DateTime\|null, updated_at: DateTime}`. `require_dual_approval`, `tax_bps` and `retirement_bps` are the calculate-time snapshots (null while `draft`) (R21, C9) | PAY-303, 413 |
| `Hours` | payroll `HoursOut` | `{pay_run_id: Uuid, status: "draft", entries: [{employee_id: Uuid, hours_hundredths: int}]}` (C7) | PAY-301 |
| `Payslip` | payroll `PayslipOut` | `{id: Uuid, pay_run_id: Uuid, employee_id: Uuid, employee_name: string, pay_type: PayType, annual_salary_cents: int\|null, hourly_rate_cents: int\|null, hours_hundredths: int\|null, status: "pending"\|"calculated", gross_cents: Cents\|null, tax_cents: Cents\|null, retirement_cents: Cents\|null, net_cents: Cents\|null}`. No `schedule`; the amounts are null while `pending` (C5) | PAY-303 |
| `Attempt` (strict) | payroll `AttemptOut` | `{attempt: int ≥ 1, status: "requested"\|"indeterminate"\|"needs_operator"\|"settled"\|"rejected", failure_reason: string\|null, bank_reference: string ≤ 64\|null, account_last4: /^\d{4}$/, requested_at: DateTime, final_at: DateTime\|null}` (C5, C26) | PAY-501 |
| `Payout` (strict) | payroll `PayoutOut` | `{id: Uuid, pay_run_id: Uuid, payslip_id: Uuid, employee_id: Uuid, employee_name: string, amount_cents: Cents, currency: "USD", status: "requested"\|"paid"\|"failed", no_transfer: boolean, current_attempt: int ≥ 0, attempts: Attempt[]}`. `attempts` is ascending by `attempt`, and `[]` when `no_transfer` (R21, C5) | PAY-500, 501, 502, 413 |
| `AuthAuditEvent` | identity `AuditEntryOut` | `{id: Uuid, at: DateTime, actor_user_id: Uuid\|null, event: string, target_user_id: Uuid\|null, ip_hash: string\|null, details: Record<string, unknown>}` | PAY-109 |
| `PayrollAuditEvent` | payroll `AuditEventOut` | `{id: Uuid, at: DateTime, actor_id: string, event: string, entity_type: "company_settings"\|"employee"\|"pay_run"\|"payout", entity_id: Uuid, details: Record<string, unknown>}` (C5) | plan §8 |
| `ValidationDetails` | the C14 shape | `z.array(z.object({field: z.string().max(200), issue: z.string().max(100)})).max(100)` | PAY-602 |
| `EmployeeIdsDetails` | payroll domain errors | `{employee_ids: z.array(Uuid).max(500)}` | PAY-300–302 |
| `ErrorBody` | PAY-602 | `{error: {code: string, message: string, request_id: string, details?: ValidationDetails \| EmployeeIdsDetails}}` | PAY-602 |

## 3. Interfaces

### 3.1 The request pipeline (`lib/route.ts`)

```ts
type Auth = "public" | "optional" | { roles?: Role[] };     // roles: any one of them suffices
type PublicLimiter = "fail_closed" | "fail_open";
interface RouteDef<B, Q, P> {
  name: RouteName;                    // the route template, e.g. "/api/pay-runs/[id]/approve"; the metric label
  auth: Auth;
  publicLimiter?: PublicLimiter;      // "fail_closed" for login, reset and accept (PAY-603)
  body?: z.ZodType<B>; query?: z.ZodType<Q>; params?: z.ZodType<P>;
  bodyLimit?: number;                 // default BFF_BODY_LIMIT_BYTES (65536)
  handler(ctx: RouteCtx<B, Q, P>): Promise<Response>;
}
interface RouteCtx<B, Q, P> {
  req: NextRequest; body: B; query: Q; params: P;
  session: Session | null; requestId: string; log: pino.Logger; ip: string | null;   // null = unknown (§5.2)
  identity: IdentityClient; payroll: PayrollClient;   // built for this request (user token, or service:web)
}
export function route<B, Q, P>(def: RouteDef<B, Q, P>):
  (req: NextRequest, ctx: { params: Promise<Record<string, string>> }) => Promise<Response>;
```

**Steps.** Each one may throw a `BffError`, which step 10 turns into a response.

1. **Context.** Read `x-request-id` from the request (`proxy.ts` set it, and accepts an incoming one matching `^[0-9a-f-]{36}$`). Otherwise generate a UUIDv7. Set `AsyncLocalStorage`. Read `traceId` from the active span.
2. **IP rate limit** (`scope=ip`, `BFF_RATE_LIMIT_IP` per minute). This applies to every route except `/api/livez`, `/api/readyz` and `/api/telemetry`, which has its own limit (§7.5).
   - If Redis fails and `publicLimiter === "fail_closed"`: 503 `service_unavailable`.
   - Otherwise the limit fails open: count `rate_limiter_errors_total{component="web"}` and log `ratelimit.backend_error`.
3. **Origin**, for POST, PUT, PATCH and DELETE (§5.4): 403 `bad_origin`.
4. **Session.**
   - `auth: {roles}` → `requireSession(roles)` (§4.3).
   - `"optional"` → load the session if the cookie is present; never throw 401.
   - `"public"` → skip.
5. **Session rate limit** (`scope=session`, `BFF_RATE_LIMIT_SESSION` per minute), when there's a session. It fails open. `/api/telemetry` never spends it: the telemetry limit is its own (C63).
6. **Params, query and headers:** Zod. A failure gives 422 `validation_error` with `details: [{field, issue}]` and no input values (C14). A malformed `[id]` also gives 422. A route that requires `Idempotency-Key` returns 400 `idempotency_key_required` when it is missing (C34).
7. **Body:** `readJson(req, def.body, def.bodyLimit)` (§5.3): 413 `payload_too_large`, 400 `invalid_json`, or 422 `validation_error`.
8. **Clients.** Build `identity` and `payroll` with a token provider bound to the session: `sub=user_id`, the session's `name`, roles, `cid` and `sid` (C10). Public routes use `sub=service:web` with no `name` or `sid` (R9).
9. **Handler.**
10. **Finish.**
    - Map any thrown error with `toErrorResponse()` (§3.3).
    - Set `x-request-id`, `x-trace-id` and `Cache-Control: no-store` on every response, including errors (PAY-604).
    - Record `bff_request_duration_seconds{route=def.name, status}`.
    - Log `http.request` (§7.1).

### 3.2 BFF route table

Conventions for the table:
- **"Origin ✓"** means step 3 applies.
- **JWT:** `U` = a user token (`sub=<user_id>`, the session's `name`, roles, `cid` and `sid`). `S` = a service token (`sub=service:web`, `roles=[]`, `cid=COMPANY_ID`, no `name`, no `sid`; R9).
- **Timeouts:** every call has a 3 s per-attempt timeout (connect and read together), except calculate and approve, which get 10 s (§3.4.2).
- **Pass-through responses** are the parsed §2.4 schema. **Lists** take `cursor` and `limit` (§2.3 `ListQuery`, plus the filters shown), pass them to the service unchanged, and return `Page<T>` (C8).
- **Error mapping:** every route also maps these:
  - 401 `unauthenticated` (no or ended session). The route handler answers it, with every standard header: `proxy.ts` doesn't gate `/api/*` (C63);
  - 403 `forbidden_role` (BFF or service);
  - 400 `invalid_json`, 400 `bad_request` (service), 403 `bad_origin`, 404 `not_found` (service), 405 `method_not_allowed`, 413 `payload_too_large`, 422 `validation_error`, 429 `rate_limited`;
  - 503 `service_unavailable` (upstream down, timeout, breaker open or Redis down), and 503 `db_timeout` / `db_unavailable` forwarded as 503 `service_unavailable`;
  - 500 `internal_error`.

  The table lists only the route-specific codes. Their messages are in §3.3.

#### Auth

| Method, path | Session / role | Origin | Request (Zod) | Service call(s) | Response | Route-specific errors (status code) | Req |
|---|---|---|---|---|---|---|---|
| POST `/api/auth/login` | public; IP limit **fails closed** | ✓ | `LoginBody` | identity `POST /v1/auth/login` (S; `X-Client-IP` when known) → 200 `SessionUser` | 200 `{user: Me}` (`toMe(SessionUser)`, C6) + `Set-Cookie`. An existing session cookie on the request is deleted first; the new session gets a fresh ID (§4.1) | 401 `invalid_credentials`; 429 `rate_limited` (identity's account/IP lockout, `Retry-After` forwarded) | PAY-104, 106 |
| POST `/api/auth/logout` | optional | ✓ | none | none | 204, cookie cleared (`Max-Age=0`). Deletes `web:sess:<k>` and `SREM web:user-sess:<user_id> <k>`. Idempotent without a session | — | PAY-106 |
| POST `/api/auth/accept` | public; **fails closed** | ✓ | `AcceptBody` | identity `POST /v1/invites/accept` (S; `X-Client-IP` when known) → 200 `SessionUser` | 200 `{user: Me}` (`toMe(SessionUser)`) + `Set-Cookie` (new session) | 400 `invite_invalid`; 422 `password_too_weak`; 429 `rate_limited` | PAY-103, 106 |
| POST `/api/auth/reset/request` | public; **fails closed** | ✓ | `ResetRequestBody` | identity `POST /v1/auth/password-reset/request` (S; `X-Client-IP` when known) → 202 | 202 `{}`, always (no account enumeration) | 429 `rate_limited` | PAY-105 |
| POST `/api/auth/reset/confirm` | public; **fails closed** | ✓ | `ResetConfirmBody` | identity `POST /v1/auth/password-reset/confirm` (S; `X-Client-IP` when known) → 200 `ResetConfirmResult`; then **at once** `revokeUserSessions(user_id, "password_reset")`, deleting every session in `web:user-sess:<user_id>` (R19). A Redis error during the revoke doesn't fail the request (the password has changed): `auth.session.revoke_failed` is logged and the `roles_version` re-check ends the sessions within 60 s, as for user PATCH (§4.4) | 204; the current cookie is cleared | 400 `reset_invalid`; 422 `password_too_weak`; 429 `rate_limited` | PAY-105 |
| GET `/api/auth/me` | any role | — | none | identity `GET /v1/users/me` (U) → 200 `UserMe`. This is also a forced re-check: the §4.3 rules apply, and `roles_checked_at`, `roles` and `name` are updated | 200 `Me` (`toMe(UserMe)`). While the re-check is deferred (identity unavailable inside the C37 grace) there's no fresh profile, and the session holds no email, so this route answers **503 `service_unavailable`** (C62) | — (identity's 404 `user_not_found` ends the session: 401) | PAY-106, 107 |

#### Users and invites (admin)

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/users` | admin | — | `ListQuery` | identity `GET /v1/users?cursor&limit` (U) | 200 `Page<User>` | — | PAY-108 |
| POST `/api/users/invite` | admin | ✓ | `InviteBody` | identity `POST /v1/users/invite` (U) | 201 `User` (`pending`, with `pending_invite_id`) | 409 `user_email_taken`; 422 `validation_error` (unknown, empty or duplicate roles) | PAY-101 |
| PATCH `/api/users/[id]` | admin | ✓ | `IdParam`, `UserPatchBody` (`status` may only be `"inactive"`; C25) | identity `PATCH /v1/users/{id}` (U); on 200, `revokeUserSessions(id, "role_change" \| "deactivated")` | 200 `User` | 409 `cannot_modify_self`; 409 `last_admin`; 404 `user_not_found` | PAY-107, 108 |
| POST `/api/invites/[id]/resend` | admin | ✓ | `IdParam` (the user's `pending_invite_id`) | identity `POST /v1/invites/{id}/resend` (U) | 200 `PendingInvite` | 409 `invite_not_pending`; 404 `invite_not_found` | PAY-102 |

#### Settings

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/settings` | any | — | none | payroll `GET /v1/company/settings` (U) | 200 `Settings` | 404 `company_settings_not_found` | PAY-200 |
| PUT `/api/settings` | admin | ✓ | `SettingsBody` | payroll `PUT /v1/company/settings` (U) | 200 `Settings` | 409 `anchor_locked` (R33); 404 `company_settings_not_found`; 422 `validation_error` (out of range) | PAY-200 |

#### Employees

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/employees` | any | — | `EmployeeListQuery` | payroll `GET /v1/employees?status&cursor&limit` (U) | 200 `Page<Employee>` | — | PAY-202 |
| POST `/api/employees` | payroll_admin | ✓ | `EmployeeCreateBody` | payroll `POST /v1/employees` (U) | 201 `Employee` | 409 `employee_email_taken`; 422 `validation_error` (fields in `details`) | PAY-201 |
| GET `/api/employees/[id]` | any | — | `IdParam` | payroll `GET /v1/employees/{id}` (U) | 200 `Employee` | 404 `employee_not_found` | PAY-202 |
| PATCH `/api/employees/[id]` | payroll_admin | ✓ | `IdParam`, `EmployeePatchBody` | payroll `PATCH /v1/employees/{id}` (U) | 200 `Employee` | 404 `employee_not_found`; 409 `employee_email_taken`; 422 `validation_error` (including reactivation without bank details after a purge) | PAY-203 |

#### Pay runs, payslips, payouts

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/pay-runs` | any | — | `PayRunListQuery` | payroll `GET /v1/pay-runs?status&cursor&limit` (U) | 200 `Page<PayRun>` (newest period first) | — | PAY-303 |
| POST `/api/pay-runs` | payroll_admin | ✓ | `PayRunCreateBody` | payroll `POST /v1/pay-runs` (U) | 201 `PayRun` | 409 `pay_run_exists`; 422 `invalid_period_start`; 409 `no_employees`; 409 `employee_in_overlapping_run` (`details.employee_ids` forwarded) | PAY-300 |
| GET `/api/pay-runs/[id]` | any | — | `IdParam` | payroll `GET /v1/pay-runs/{id}` (U) | 200 `PayRun`. Polled every 2 s while `processing`; `counts.awaiting_bank` and `counts.blocked` drive the run banner (R21) | 404 `pay_run_not_found` | PAY-303, 413 |
| DELETE `/api/pay-runs/[id]` | payroll_admin | ✓ | `IdParam` | payroll `DELETE /v1/pay-runs/{id}` (U) | 204 | 409 `pay_run_locked`; 404 `pay_run_not_found` | PAY-304 |
| PUT `/api/pay-runs/[id]/hours` | payroll_admin | ✓ | `IdParam`, `HoursBody` | payroll `PUT /v1/pay-runs/{id}/hours` (U) → 200 `HoursOut` | 200 `Hours` (C7). The run is now `draft`; the UI refetches the run and its payslips | 404 `pay_run_not_found`; 409 `pay_run_locked`; 422 `not_hourly` and 422 `employee_not_in_run` (both with `details.employee_ids` forwarded); 422 `validation_error` (bounds) | PAY-301 |
| POST `/api/pay-runs/[id]/calculate` | payroll_admin | ✓ | `IdParam` | payroll `POST /v1/pay-runs/{id}/calculate` (U; **read 10 s**) | 200 `PayRun` (`calculated`) | 404 `pay_run_not_found`; 422 `hours_missing` (`details.employee_ids` forwarded); 409 `pay_run_locked` | PAY-302 |
| POST `/api/pay-runs/[id]/approve` | approver | ✓ | `IdParam`; header `Idempotency-Key: IdempotencyKeyHeader` (required: **missing → 400 `idempotency_key_required`**, not a UUID → 422 `validation_error`; C34) | payroll `POST /v1/pay-runs/{id}/approve` (U; **read 10 s**; `Idempotency-Key` forwarded unchanged; never retried by the BFF) | 200 `PayRun` (`processing` or `paid`) | 404 `pay_run_not_found`; 409 `pay_run_not_approvable`; 403 `approver_must_differ` (approve message); 409 `employee_bank_details_missing` (approve message; R34); 422 `idempotency_key_reused` | PAY-400, 401 |
| GET `/api/pay-runs/[id]/payslips` | any | — | `IdParam`, `ListQuery` | payroll `GET /v1/pay-runs/{id}/payslips?cursor&limit` (U) | 200 `Page<Payslip>` (by employee name; C8) | 404 `pay_run_not_found` | PAY-303 |
| GET `/api/pay-runs/[id]/payslips/[payslipId]` | any | — | `RunPayslipParam` | payroll `GET /v1/pay-runs/{id}/payslips/{payslip_id}` (U) | 200 `Payslip` | 404 `pay_run_not_found`; 404 `payslip_not_found` | PAY-303 |
| GET `/api/pay-runs/[id]/payouts` | any | — | `IdParam`, `PayoutListQuery` | payroll `GET /v1/pay-runs/{id}/payouts?status&cursor&limit` (U) | 200 `Page<Payout>` (by employee name; C8) | 404 `pay_run_not_found` | PAY-501, 413 |
| POST `/api/pay-runs/[id]/payouts/[payoutId]/retry` | approver | ✓ | `RunPayoutParam`; header `Idempotency-Key` (a UUID per click, as for approve; C52): missing → 400 `idempotency_key_required`, not a UUID → 422 `validation_error` (C34) | payroll `POST /v1/pay-runs/{id}/payouts/{payout_id}/retry` (U; `Idempotency-Key` forwarded; never retried by the BFF) | 200 `Payout` | 409 `payout_not_retryable`; 409 `retry_limit_reached`; 409 `employee_bank_details_missing` (retry message); 403 `approver_must_differ` (retry message); 404 `pay_run_not_found`; 404 `payout_not_found` | PAY-500 |
| POST `/api/pay-runs/[id]/payouts/[payoutId]/resolve` | approver | ✓ | `RunPayoutParam`, `ResolveBody` | payroll `POST /v1/pay-runs/{id}/payouts/{payout_id}/resolve` (U; read 3 s; never retried). The BFF forwards `{action, note}` unchanged and never logs `note` (only its length) | 200 `Payout` (`redrive`: current attempt `requested`, run `processing`; `mark_rejected`: attempt `rejected` with `failure_reason = "operator_rejected"`, payout `failed`, so Retry opens) | 409 `payout_not_blocked`; 409 `resolve_too_early` (the BFF forwards `Retry-After`); 404 `pay_run_not_found`; 404 `payout_not_found`; 422 `validation_error` (unknown action, missing or long note) | PAY-502 |

#### Audit and infrastructure

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/audit/auth` | admin | — | `ListQuery` | identity `GET /v1/audit-log?cursor&limit` (U) | 200 `Page<AuthAuditEvent>` (newest first) | — | PAY-109 |
| GET `/api/audit/payroll` | admin | — | `ListQuery` | payroll `GET /v1/audit-events?cursor&limit` (U) | 200 `Page<PayrollAuditEvent>` (newest first) | — | plan §8 |
| POST `/api/telemetry` | optional; own limit `BFF_TELEMETRY_RATE_LIMIT_IP`, failing open; never the session limit (C63) | ✓ | `TelemetryBody`, limit 8 KB | none | 204 | 429 `rate_limited` | §7.5 |
| GET `/api/livez` | public, no limit | — | none | none | 200 `{status:"ok"}` | — | PAY-605 (web) |
| GET `/api/readyz` | public, no limit | — | none | Redis `PING` | 200 `{status:"ok"}`, or 503 `service_unavailable` | — | PAY-605 (web) |
| any method, unknown `/api/*` path | public, IP limit | — | none | none | — | 404 `not_found` | PAY-602 |

**Approve idempotency (PAY-400).**
- `approve-button.tsx` creates `crypto.randomUUID()` in the click handler, once per click. The key is stored in the mutation's variables, so a re-render can't change it.
- The button is disabled while the mutation is pending. A double-click therefore sends one request.
- A second, deliberate click after a failure sends a new key. payroll answers 409 `pay_run_not_approvable` if the first one landed.
- **Mutations are never retried** by TanStack Query (`retry: 0`) or by the BFF.
- **On a network error** (no response), the page refetches `GET /api/pay-runs/{id}`. If the status is `processing` or `paid`, the UI shows success. Otherwise it shows the error and lets the user click again.

### 3.3 Error mapping (`lib/errors.ts`)

Every error response is `{error: {code, message, request_id, details?}}` (PAY-602).
- **Known service codes** keep their status and code. The **message comes from the BFF catalog**, never from the service.
- **Unknown codes:**
  - an unknown 4xx keeps its status and becomes code `request_failed` with a generic message, logged as `bff.unmapped_error` at warning;
  - an unknown 5xx becomes 500 `internal_error`.
- **`details`** is forwarded only in these cases, and always re-parsed first; if it doesn't parse, the response has no `details`:
  - 422 `validation_error`: parsed as `ValidationDetails` (`[{field, issue}]`; C14). Extra keys are stripped, so no submitted value can pass through;
  - `not_hourly`, `employee_not_in_run`, `employee_in_overlapping_run` and `hours_missing`: parsed as `EmployeeIdsDetails`, and only `employee_ids` is forwarded (C15).
- **Internals are never forwarded:** no upstream message, stack trace, URL or SQL.

| Code | Status | User-facing message | Source |
|---|---|---|---|
| `unauthenticated` | 401 | "Your session has ended. Please sign in again." | BFF (PAY-106) |
| `forbidden_role` | 403 | "You don't have permission to do that." | BFF, services (PAY-601) |
| `bad_origin` | 403 | "This request was blocked for your security. Reload the page and try again." | BFF (PAY-604) |
| `payload_too_large` | 413 | "That request is too large." | BFF, services (PAY-604) |
| `invalid_json` | 400 | "The request couldn't be read." | BFF (R3) |
| `bad_request` | 400 | "The request couldn't be processed." | services (C15) |
| `not_found` | 404 | "That page or item wasn't found." | BFF (unknown `/api/*` path), services (C15) |
| `method_not_allowed` | 405 | "That action isn't supported here." | BFF, services (C15) |
| `idempotency_key_required` | 400 | "This action was sent without its safety key. Reload the page and try again." | BFF, payroll (C34, PAY-400) |
| `validation_error` | 422 | "Some fields need attention." (plus `details`) | BFF and services (R3, C14) |
| `rate_limited` | 429 | "Too many requests. Try again in {Retry-After} seconds." (`Retry-After` forwarded) | BFF, identity, services (PAY-603, PAY-104) |
| `service_unavailable` | 503 | "The service is temporarily unavailable. Please try again." | BFF (upstream down, timeout, breaker, Redis) (PAY-602, PAY-603) |
| `db_timeout`, `db_unavailable` (upstream) | 503 → `service_unavailable` | as `service_unavailable` | payroll, identity (PAY-605) |
| `invalid_token` (upstream 401) | → 500 `internal_error` | "Something went wrong. Reference: {request_id}." Logged as `bff.upstream_auth_failed` at error: our signing key or clock is wrong, and the user can't fix it | services (PAY-600) |
| `internal_error` | 500 | "Something went wrong. Reference: {request_id}." | all (PAY-602) |
| `invalid_credentials` | 401 | "Email or password is incorrect." (on the login route this doesn't end a session or redirect) | identity (PAY-104) |
| `invite_invalid` | 400 | "This invite link has expired or was already used. Ask your admin to send a new one." | identity (PAY-103) |
| `password_too_weak` | 422 | "Use a password of at least 12 characters." | identity (PAY-103, 105) |
| `reset_invalid` | 400 | "This reset link has expired or was already used. Request a new one." | identity (PAY-105) |
| `user_email_taken` | 409 | "A user with this email already exists." | identity (PAY-101) |
| `invite_not_pending` | 409 | "This user has already accepted their invite." | identity (PAY-102) |
| `cannot_modify_self` | 409 | "You can't change your own roles or status. Ask another admin." | identity (PAY-108) |
| `last_admin` | 409 | "The company must keep at least one active admin." | identity (PAY-108) |
| `user_not_found`, `invite_not_found` | 404 | "That user wasn't found." / "That invite wasn't found." | identity (R3, PAY-602) |
| `company_settings_not_found` | 404 | "Company settings haven't been set up yet. Ask an admin." | payroll (C15, PAY-200) |
| `anchor_locked` | 409 | "The biweekly anchor can't change after biweekly runs exist." | payroll (R33, PAY-200) |
| `employee_email_taken` | 409 | "An employee with this email already exists." | payroll (PAY-201) |
| `employee_not_found` | 404 | "That employee wasn't found." | payroll (PAY-202) |
| `pay_run_exists` | 409 | "A pay run already exists for this schedule and period." | payroll (PAY-300) |
| `invalid_period_start` | 422 | Monthly: "Monthly runs must start on the 1st of a month." Biweekly: "Biweekly runs must start on a period start date (every 14 days from {biweekly_anchor})." | payroll (PAY-300) |
| `no_employees` | 409 | "No active employees are on this schedule." | payroll (PAY-300) |
| `employee_in_overlapping_run` | 409 | "{n} employee(s) are already in another run that covers these dates." (lists them) | payroll (PAY-300) |
| `pay_run_locked` | 409 | "This run is locked because it has been approved." | payroll (PAY-301, 302, 304) |
| `not_hourly` | 422 | "Hours can only be entered for hourly employees." (the rows in `details.employee_ids` are highlighted) | payroll (PAY-301) |
| `employee_not_in_run` | 422 | "{n} employee(s) aren't in this run. Reload the run and try again." | payroll (C15, PAY-301) |
| `hours_missing` | 422 | "Enter hours for every hourly employee before calculating ({n} missing)." | payroll (PAY-302) |
| `pay_run_not_approvable` | 409 | "This run can't be approved: it has already been approved, or it isn't calculated." | payroll (PAY-400) |
| `idempotency_key_reused` | 422 | "This approval was already submitted with different details. Reload and try again." | payroll (PAY-400) |
| `approver_must_differ` (approve route) | 403 | **"Dual approval is on: you can't approve this run because you calculated it, or you changed bank details for an employee in it. Ask another approver."** | payroll (PAY-401, ADR 0005) |
| `approver_must_differ` (retry route) | 403 | **"Dual approval is on: you can't retry this payout because you last changed this employee's bank details. Ask another approver."** | payroll (PAY-500, ADR 0005) |
| `payout_not_retryable` | 409 | "Only failed payouts can be retried. Payouts awaiting the bank or blocked can't be retried." | payroll (PAY-500) |
| `retry_limit_reached` | 409 | "This payout has reached the limit of 5 attempts." | payroll (PAY-500) |
| `employee_bank_details_missing` (approve route) | 409 | "Some employees in this run have no bank details. Add their bank details, then approve." | payroll (PAY-400, R34) |
| `employee_bank_details_missing` (retry route) | 409 | "This employee is inactive or has no bank details. Update the employee first." | payroll (PAY-500) |
| `payout_not_blocked` | 409 | "This payout isn't blocked any more. Refresh the page." | payroll (PAY-502) |
| `resolve_too_early` | 409 | "This payout was blocked moments ago. Wait {Retry-After} seconds, check the bank again, then mark it rejected." (`Retry-After` forwarded) | payroll (PAY-502, C48) |
| `pay_run_not_found`, `payslip_not_found`, `payout_not_found` | 404 | "That pay run / payslip / payout wasn't found." | payroll (R3, PAY-602) |
| `unsupported_media_type` | 415 | "That request format isn't supported." | services (pycommon generic 415; C63) |
| `conflict` | 409 | "That change conflicts with the current state. Reload and try again." | services (pycommon and payroll generic 409; C63) |
| `request_failed` | as upstream (4xx) | "The request couldn't be completed." | BFF fallback |

`mapUpstreamError(target, res | err, routeName)` is a pure function with one Vitest case per row.

### 3.4 Calling services

#### 3.4.1 Internal JWT (`lib/internal-jwt.ts`, PAY-600)

```ts
import "server-only";
import { SignJWT, importPKCS8 } from "jose";
type Aud = "identity" | "payroll";
let keyPromise: ReturnType<typeof importPKCS8> | undefined;          // lazy: `next build` has no secrets
const signingKey = () => (keyPromise ??= importPKCS8(config().INTERNAL_JWT_PRIVATE_KEY, "EdDSA"));

export async function internalToken(aud: Aud, p:
  { sub: string; roles: Role[]; cid: string; sid?: string; name?: string }): Promise<string> {
  return new SignJWT({ roles: p.roles, cid: p.cid,
      ...(p.sid !== undefined ? { sid: p.sid } : {}),
      ...(p.name !== undefined ? { name: p.name } : {}) })            // user tokens only (R9, C10)
    .setProtectedHeader({ alg: "EdDSA", kid: config().INTERNAL_JWT_KID, typ: "JWT" })
    .setIssuer("web").setAudience(aud).setSubject(p.sub)   // user id, or "service:web" on public flows
    .setIssuedAt().setExpirationTime("60s").setJti(crypto.randomUUID())
    .sign(await signingKey());
}
```

- **Claims:** exactly `iss=web`, `aud` (`identity` or `payroll`), `sub`, `roles`, `cid`, `sid` and `name` (user tokens only), `iat`, `exp = iat + 60`, `jti` (PAY-600).
- **A fresh token per upstream call.** Ed25519 signing costs about 50 µs. There's no cache, so a token never outlives its request by much.
- **User tokens:** `sub=<user_id>`; `name`, roles and `cid` from the session; `sid` = `session.sid`. payroll stores `name` as `calculated_by_name` / `approved_by_name` (R9, C10).
- **Public flows** (login, accept, reset request and confirm): `sub=service:web`, `roles=[]`, `cid=COMPANY_ID`, and no `name` or `sid` (R9).
- **Test and CI tokens** are minted outside the app by `scripts/mint_token.py` (C35), never by a test-only route in the web.
- **Never logged.** The `authorization` key is redacted (§7.1), and a test asserts that no captured log line contains the token.
- **Key rotation** (`auth.md`): add the new public key to every service's `INTERNAL_JWT_PUBLIC_KEYS` first, then change `INTERNAL_JWT_PRIVATE_KEY` and `INTERNAL_JWT_KID`, then remove the old key after 60 s.

#### 3.4.2 Timeouts, retries and breaker (`lib/api/resilient-fetch.ts`, `lib/api/breaker.ts`)

`openapi-fetch` clients are created per request (`lib/api/clients.ts`) with `createIdentityClient({ baseUrl: IDENTITY_BASE_URL, fetch: resilientFetch("identity", opts) })`; payroll is the same. Calculate and approve pass `fetch: slowPayrollFetch(ctx)` per call. A middleware (`client.use`) adds `Authorization` and `X-Request-ID`, and adds `X-Client-IP` only when `ctx.ip` isn't null (C16).

| Policy | Value | Source |
|---|---|---|
| Timeout, per attempt | `UPSTREAM_READ_TIMEOUT_MS` = 3000; `UPSTREAM_SLOW_READ_TIMEOUT_MS` = 10000 for `calculate` and `approve`. **One timer bounds connect and read together**: there's no separate connect timeout and no `UPSTREAM_CONNECT_TIMEOUT_MS` key, because an `undici` 8 dispatcher needs Node ≥ 24 (C62). It's an explicit `setTimeout` plus an `AbortController`, not `AbortSignal.any([... AbortSignal.timeout(ms)])`: the latter's source signals hold their dependents weakly, so a pending attempt could be garbage-collected (NOTES.md). The timeout aborts with a `TimeoutError` | plan §4 |
| Retries | **GET only**, at most `UPSTREAM_GET_RETRIES` = 2 (so 3 attempts). Retried on HTTP **429, 502, 503 and 504**, and on connection failures where nothing was sent (`ECONNREFUSED`, `ECONNRESET` before the request was written, connect timeout). A read timeout isn't retried. POST, PUT, PATCH and DELETE are never retried | plan §4 |
| Backoff | Full jitter: `random(0, min(1000, 100 × 2^n))` ms. A `Retry-After` ≤ 1 s is honoured; a longer one isn't retried, and 429 `rate_limited` goes back with that header. No retry starts after `UPSTREAM_GET_DEADLINE_MS` (5000) from the first attempt | `architecture.md` §4 |
| Breaker | One per target (`identity`, `payroll`) per process: **closed** → **open** after `BREAKER_FAILURES` (5) consecutive failures. Failures are connect errors, timeouts, 502, 503 and 504; 4xx and 500 don't count. **open** → **half-open** after `BREAKER_RESET_S` (30). In half-open, one probe request runs and the rest fail fast; a success closes the breaker and a failure re-opens it. A probe the caller aborts gives no verdict: `breaker.release()` frees the probe slot, so the breaker can't stick in half-open (C63). While it's open, calls fail immediately with 503 `service_unavailable`, with no network call | plan §4, §2.2 |
| Fallback | 503 `service_unavailable` with the mapped message. **Never a cached or stale response**: the BFF caches no service data | plan §4 |

Each attempt:
- increments `bff_upstream_requests_total{target, outcome}`;
- logs `bff.upstream_retry` (warning) when retried and `bff.upstream_error` (warning) when the final result is a failure;
- on a breaker transition, logs `external.breaker.opened` / `external.breaker.closed` and sets `circuit_breaker_state{target}`.

## 4. Sessions (`lib/session.ts`, `lib/require-session.ts`)

### 4.1 Creating a session (login and accept)

1. identity returns 200 `SessionUser {user_id, company_id, email, name, roles, roles_version, status}` (R19, C6). The BFF parses it with the `SessionUser` Zod schema; a parse failure is 500 `internal_error` plus `bff.response_invalid`, and no session is created.
2. **Rotation.** If the request carried a session cookie, delete that session (`DEL web:sess:<old k>`, `SREM`). Then generate a new ID: `randomBytes(32).toString("base64url")`. A session ID is never reused across a sign-in (PAY-106; prevents fixation).
3. `k = HMAC(SESSION_SECRET, id)`, `now = Date.now()`. Run as one `MULTI`:
   - `HSET web:sess:<k> user_id <user_id> cid <company_id> name <name> roles <JSON roles> roles_version <n> created_at now last_seen now roles_checked_at now`;
   - `EXPIRE web:sess:<k> SESSION_IDLE_TTL_S`;
   - `SADD web:user-sess:<user_id> <k>`;
   - `EXPIRE web:user-sess:<user_id> SESSION_ABS_TTL_S`.
4. Set the cookie (§2.2). Log `auth.session.created {user_id_hash}` and increment `bff_sessions_created_total`. Respond `{user: toMe(sessionUser)}`.
5. If Redis fails at step 3: 503 `service_unavailable`, with no cookie set. This route was already fail-closed at its limiter.

### 4.2 TTLs and the touch

- **Idle TTL.** A request refreshes the idle TTL only when `now − last_seen ≥ SESSION_TOUCH_INTERVAL_S × 1000` (60 s), so a busy page doesn't write to Redis on every call. The touch is `HSET last_seen now` plus `PEXPIRE web:sess:<k> min(SESSION_IDLE_TTL_S×1000, created_at + SESSION_ABS_TTL_S×1000 − now)`. So idle expiry is accurate to within 60 s of 12 h.
- **Absolute TTL.** The TTL never runs past `created_at + SESSION_ABS_TTL_S`. `loadSession` also checks `now − created_at > SESSION_ABS_TTL_S×1000`; if so, it deletes the session and returns null, as a second guard.
- An expired session behaves like a missing one: 401 `unauthenticated` for `/api/*`, a redirect to `/login` for pages, and the cookie is cleared (PAY-106).
- **Stale index entries.** `web:user-sess:<user_id>` can hold keys that have already expired. `revokeUserSessions` tolerates that (a `DEL` of a missing key is a no-op), and session creation runs `SREM` for members whose `EXISTS` is 0, at most 20 at a time.

### 4.3 `requireSession(roles?)`

```ts
export async function resolveSession(cookie: string | undefined, o?: { roles?: Role[]; ip?: string | null }): Promise<CheckedSession>
  // the algorithm; route() calls it with the request's cookie and client IP. Throws BffError
export async function requireSession(roles?: Role[]): Promise<CheckedSession>
  // reads cookies() and headers() (client IP per §5.2, so a re-check sends X-Client-IP; C16)
export async function requireSessionForPage(path: string, roles?: Role[]): Promise<CheckedSession | "forbidden">
  // pages: redirect("/login?next=<path>") on 401; returns "forbidden" (the page renders <Forbidden/>) on 403;
  // throws on 503 (error.tsx shows "temporarily unavailable")
```

Algorithm (PAY-106, PAY-107, PAY-601, PAY-603):

1. Read the cookie `SESSION_COOKIE_NAME` (`req.cookies` in `route()`, `await cookies()` elsewhere). If it's missing → 401 `unauthenticated`.
2. `k = HMAC(SESSION_SECRET, cookie)`. Then `HGETALL web:sess:<k>`.
   - **Redis error or timeout** (`REDIS_SOCKET_TIMEOUT_MS` = 500) → 503 `service_unavailable`. Log `session.backend_error` (error) and increment `bff_session_backend_errors_total`. Never fall back to "no session", and never fall back to "allow".
   - **Empty** → 401 `unauthenticated`, and the response clears the cookie.
3. Parse with the `SessionRecord` Zod schema. If it's corrupt → delete it, then 401.
4. **Absolute check** (§4.2) → 401 if past.
5. **Role and status re-check**, when `now − roles_checked_at ≥ SESSION_ROLE_RECHECK_S × 1000` (60 s):
   - Call identity `GET /v1/users/me` with a user token (`sub=user_id`, the session's `name`, roles, `cid`, `sid`) and parse the 200 body as `UserMe`. This is single-flighted per `k` within the process, so parallel requests from one page share one call.
   - **200, and `id === session.user_id`, `company_id === session.cid`, `status === "active"` and `roles_version === session.roles_version`** → `HSET roles_checked_at now roles <JSON roles> name <name>`, writing `roles` and `name` from the response (C10).
   - **200 with a different `roles_version`, or a status other than `active`** → `revokeUserSessions(user_id, "roles_version_mismatch" | "inactive")`, log `auth.session.revoked`, then **401 `unauthenticated`**. The user signs in again and gets a new session with the new roles. If those roles no longer include the one an action needs, that action then returns 403 `forbidden_role`. So a demotion or deactivation always ends the session with 401 (PAY-107), and 403 can follow only after the user signs in again with fewer roles.
   - **200 with a different `id` or `company_id`** (should never happen) → log `bff.response_invalid`, revoke this session, then 401.
   - **401 `invalid_token` from identity** (our signing key, kid or clock is wrong; the user can't fix it) → log `bff.upstream_auth_failed` (error) and answer **503 `service_unavailable`**, keeping the session and skipping the grace (C63).
   - **Any other 401, or 404, from identity** (user gone) → delete this session, then 401.
   - **identity 500 or another 5xx** that isn't 503 → 503 `service_unavailable` at once, with no fail-open grace (C63).
   - **Another 4xx** → 500 `internal_error` (a bug on our side).
   - **identity unavailable**: only a 503, a 429, a timeout, a connect error or an open breaker (C63):
     - If `now − roles_checked_at < SESSION_RECHECK_GRACE_S × 1000` (300 s), **fail open**: keep the cached roles, don't update `roles_checked_at`, and log `auth.session.recheck_deferred` (warning). This keeps signed-in journeys working while identity is down (plan §12.1, "other journeys still work for signed-in users"). It's safe because no role change or reset can happen while identity is down.
     - **Past the grace** → 503 `service_unavailable` (C37, PAY-107). A change made just before identity went down then takes effect at most `SESSION_RECHECK_GRACE_S` later. While identity is reachable, a demotion or deactivation at time T gives 401 on any request after T + 60 s (PAY-107).
6. **Touch** (§4.2). A Redis error here is ignored and logged once, at warning.
7. **Roles.** If `roles` is given and the session holds none of them → 403 `forbidden_role`. The session stays valid.
8. Return the `Session` (with `sid = k.slice(0, 32)`) and put it on the request context.

### 4.4 Revocation (`web:user-sess`)

```ts
export async function revokeUserSessions(userId: string, reason: RevokeReason): Promise<number>
// RevokeReason = "role_change" | "deactivated" | "password_reset" | "roles_version_mismatch" | "inactive" | "logout"
```

1. `SMEMBERS web:user-sess:<userId>`.
2. In one `MULTI`: `DEL web:sess:<k>` for each member, then `DEL web:user-sess:<userId>`.
3. Log `auth.session.revoked {user_id_hash, reason, count}` and add to `bff_sessions_revoked_total{reason}`.

It's called:
- after a successful `PATCH /api/users/[id]` that changes `roles` or `status` (the target user's sessions; the next request is 401 at once, without waiting for the 60 s re-check);
- after a successful reset confirm, with the `user_id` from `ResetConfirmResult`, at once (R19);
- by `requireSession` on a `roles_version` mismatch or a non-active status.

Sign-out removes only the current session (`DEL` plus `SREM`).

**Revocation doesn't rely on this alone.** If the BFF crashes between identity's 200 and the revoke, or a change happens outside the BFF, the 60 s re-check (§4.3 step 5) catches it, because identity bumps `roles_version` on role changes, deactivation and password resets (PAY-105, PAY-108).

**If Redis fails during the revoke** after identity has already applied a role change:
- the route still returns identity's 200 (the change is done);
- it logs `auth.session.revoke_failed` (error);
- the re-check ends the session within 60 s once Redis is back.

### 4.5 When Redis is down (PAY-603, plan §2.1)

| Path | Behaviour | Signal |
|---|---|---|
| Any route that needs a session (all `/api/*` except public ones) | `requireSession` → 503 `service_unavailable`; pages show the error boundary ("temporarily unavailable, try again") | `session.backend_error`, `bff_session_backend_errors_total` |
| login, accept, reset request, reset confirm | The IP limiter **fails closed** → 503 `service_unavailable`, before identity is called | `ratelimit.backend_error`, `rate_limiter_errors_total{component="web"}` |
| Session and IP limits on authenticated routes; the telemetry limit | **Fail open** (counted). In practice an authenticated route still 503s at the session lookup; the fail-open matters only when the limiter call fails and the session read succeeds (a flapping connection) | `rate_limiter_errors_total{component="web"}` |
| `/api/readyz` | 503, so Kubernetes stops routing to the pod (`/api/livez` stays 200, so it isn't restarted) | — |
| logout | Best effort: the cookie is cleared, a Redis error is logged, and the response is 204 | `session.backend_error` |

The `ioredis` options (`enableOfflineQueue: false`, `commandTimeout` 500 ms) bound each failure to about 500 ms, so a Redis outage gives fast 503s rather than hung requests.

## 5. Security controls

### 5.1 Rate limiting (`lib/rate-limit.ts`, PAY-603)

```ts
export async function limit(scope: "ip" | "session" | "telemetry", subject: string, perMinute: number):
  Promise<{ ok: true } | { ok: false; retryAfterS: number }>      // throws RateLimiterBackendError on Redis error
```

- **Algorithm: a sliding window built from two fixed windows.**
  1. `w = floor(now_s/60)`. In one pipeline: `INCR web:rl:<scope>:<subj>:<w>`, `EXPIRE … 120`, `GET web:rl:<scope>:<subj>:<w−1>`.
  2. `estimate = curr + prev × (1 − (now_s mod 60)/60)`.
  3. If `estimate > perMinute`, deny with `Retry-After = 60 − (now_s mod 60)` (rounded up, at least 1).

  It's shared across replicas through Redis. Plain `INCR` and `GET` mean no `EVAL` is needed (ACL §2.1).
- **Subjects:** `ip` → `HMAC16(clientIp ?? "unknown")`; `session` → `HMAC16(k)`; `telemetry` → `HMAC16(clientIp ?? "unknown")`. No raw IP is stored in Redis. Requests with an unknown IP share one bucket.
- **Limits:**
  - `BFF_RATE_LIMIT_IP` (300/min) on every `/api/*` route except health and telemetry;
  - `BFF_RATE_LIMIT_SESSION` (120/min) after the session check;
  - `BFF_TELEMETRY_RATE_LIMIT_IP` (60/min) on telemetry only.

  The test environment (`compose.test.yaml`) raises the first two to 1000000 so k6 at 30 req/s from one IP isn't throttled. A Vitest integration test proves that the production values limit.
- **Denied** → 429 `rate_limited` with `Retry-After`. Increment `rate_limited_total{scope}` and log `ratelimit.limited` (info: scope, route).
- **Redis error** → see §4.5. Each route sets `fail_closed` or `fail_open`.
- **The polling budget:** one run page polls the run every 2 s while processing (30 requests a minute), plus the visible payouts page every 2 s while the Payouts tab is open (§6.3), so at most 60 a minute, inside 120. Two open run pages on the Payouts tab in one session reach the limit; the 429 is shown as "Live updates paused", and polling resumes after `Retry-After`.
- **Login abuse limits** (`AUTH_LOGIN_LIMIT_ACCOUNT` / `_IP`, lockout) are identity's (PAY-104). The BFF forwards identity's 429 and `Retry-After` unchanged. identity takes the client IP for the per-IP limit and `ip_hash` from `X-Client-IP` (R18), which the BFF omits when the IP is unknown (C16).

### 5.2 Client IP (`lib/client-ip.ts`)

```ts
export function clientIp(h: Headers, hops: number): string | null     // null = unknown
```

- **How Next 16 fills the header** (the R36 spike, `apps/web/NOTES.md`; C62): the standalone server sets `X-Forwarded-For` to the socket peer **only when the request has none**, and never appends. `NextRequest` has no `ip`, and a route handler can't reach the socket.
- Split `X-Forwarded-For` on commas and trim each entry; `n` is the number of entries. Each of the `hops` trusted proxies appends the address it saw, so:
  - `hops ≥ 1`: the client is entry **`n − hops`** (0-based); with one ingress that's the right-most entry;
  - `hops = 0` (dev, no proxy): the right-most entry (`n − 1`). That's the peer when the client sent no header, which is the normal case for browsers, Playwright and k6. A client that sends its own header picks its limiter bucket, which is acceptable in dev only.
- If the index is negative, or the header is missing, return `null`. Unknown IPs share one limiter bucket (§5.1), the identity client **omits `X-Client-IP`** (C16), and it's logged at most once per minute as `security.client_ip_unknown` (warning).
- The left-most value is never trusted on its own (`architecture.md` §9).
- **uat and prod refuse `TRUSTED_PROXY_HOPS < 1`** at startup (`config.invalid`, exit 2; §8), because `hops = 0` would trust a client-supplied value. `TRUSTED_PROXY_HOPS = 1` there (one ingress).
- An IPv4-mapped IPv6 address (`::ffff:a.b.c.d`) is unwrapped to IPv4.
- An entry that isn't a valid IPv4 or IPv6 address is treated as unknown (`null`).

### 5.3 Body limits (`lib/body.ts`, PAY-604)

```ts
export async function readJson<T>(req: Request, schema: z.ZodType<T>, limit = config().BFF_BODY_LIMIT_BYTES): Promise<T>
```

1. If `Content-Length` > `limit` → 413 `payload_too_large` without reading the body.
2. Otherwise read `req.body` as a stream, counting bytes. Past `limit` → cancel the stream and return 413. This covers chunked or understated bodies.
3. Require `Content-Type: application/json` (with an optional charset); otherwise 415 is folded into 422 `validation_error`.
4. Decode UTF-8 and run `JSON.parse`; a failure gives 400 `invalid_json`. Then `schema.safeParse`; a failure gives 422 `validation_error`.

Routes without a body schema reject a non-empty body with 422. Telemetry uses `limit = 8192`. Rejected requests are logged as `security.payload_too_large` (route, bytes).

### 5.4 Origin check (`lib/origin.ts`, PAY-604)

- Mutations are POST, PUT, PATCH and DELETE.
- `Origin` must equal `new URL(PUBLIC_BASE_URL).origin` exactly: scheme, host and port. In dev and CI that is `http://127.0.0.1:${WEB_PORT}` (C12), so a browser on `http://localhost:4100` gets 403.
- A missing `Origin`, `Origin: null`, or any other value gives 403 `bad_origin` and logs `security.bad_origin` (route, plus the origin's host only).
- This runs for public mutations too (login, accept, reset, telemetry, logout).
- Together with `SameSite=Lax` it is the CSRF defence (plan §8). k6 and the Playwright `page.request` helpers send `Origin: BASE_URL`.

### 5.5 `src/proxy.ts`: interceptor and CSP nonce (ADR 0009)

```ts
export function proxy(req: NextRequest): NextResponse
export const config = {
  matcher: [{ source: "/((?!_next/static|_next/image|favicon.ico|robots.txt).*)",
              missing: [{ type: "header", key: "next-router-prefetch" }, { type: "header", key: "purpose", value: "prefetch" }] }],
};
```

1. `requestId` = the incoming `x-request-id` if it matches `^[0-9a-f-]{36}$`; otherwise a new UUIDv7.
2. `nonce = base64(crypto.getRandomValues(new Uint8Array(16)))`. `csp = buildCsp(nonce, ENV, PUBLIC_BASE_URL)`. The proxy reads `ENV`, `SESSION_COOKIE_NAME` and `PUBLIC_BASE_URL` from `process.env` at run time and imports neither `lib/config.ts` nor Redis.
3. **`/api/*` is never gated here** (C63). Each route handler runs `requireSession` and answers 401 `unauthenticated` itself, with `x-request-id`, `x-trace-id` and `Cache-Control: no-store` (PAY-604), and an unknown `/api/*` path gets 404 `not_found` from the fallback route.
4. **Cookie gate for pages.** Open pages are `/login`, `/accept` and `/reset`. For any other page, if the cookie `SESSION_COOKIE_NAME` is absent, redirect 307 to `/login?next=<pathname+search>`, keeping the path only and dropping any fragment.

   Presence only: there's **no Redis lookup and no role check here** (PAY-601).
5. Otherwise `NextResponse.next({ request: { headers } })`, with the request headers `x-nonce`, `x-request-id`, `x-pathname` (path plus search, so the `(app)` layout can build `next`) and `Content-Security-Policy: csp`. Next.js reads the CSP from the request to put the nonce on its own scripts.
6. **Response headers:** `Content-Security-Policy: csp`, `x-request-id`, and in prod `Strict-Transport-Security` (§5.6).

`buildCsp(nonce, env)` returns:

```text
default-src 'self';
script-src 'self' 'nonce-<nonce>' 'strict-dynamic'[ 'unsafe-eval' in dev only];
style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:;
font-src 'self';
connect-src 'self'[ ws: in dev only];
object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none';
[upgrade-insecure-requests when PUBLIC_BASE_URL is https]
```

The app has no inline scripts of its own and no third-party origins. If one is ever needed, it gets the nonce through `(await headers()).get("x-nonce")`.

### 5.6 Static security headers (`next.config.ts` `headers()`, every path)

| Header | Value | Env |
|---|---|---|
| `X-Frame-Options` | `DENY` | all |
| `X-Content-Type-Options` | `nosniff` | all |
| `Referrer-Policy` | `same-origin` | all |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | prod only (`ENV=prod`). Set by `proxy.ts`, not `headers()`: `ENV` is a run-time value and the build has none (C62) |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=()` | all |
| `Cross-Origin-Opener-Policy` | `same-origin` | all |

The route wrapper adds `Cache-Control: no-store` to every `/api/*` response. Pages are dynamic and also get `no-store` from Next.js. The required headers are PAY-604 and plan §8; `Permissions-Policy` and COOP are additions.

## 6. UI

### 6.1 Shared behaviour

- **QueryClient defaults:**
  - `staleTime: 10_000`, `refetchOnWindowFocus: true`;
  - queries: `retry: (n, e) => n < 1 && (!(e instanceof ApiError) || e.status >= 500)`, so one retry and only on network errors or 5xx (the BFF already retried upstream);
  - mutations: `retry: 0`.
- **401 on any `/api` call:** `apiFetch` sends the user to `/login?next=<current path>`.
- **Error display:**
  - 403 `forbidden_role` → a toast with the mapped message;
  - 503 → a banner, "The service is temporarily unavailable", with a Retry button;
  - other errors → a toast with the mapped message;
  - 422 → errors shown inline, per field, from `details`.

  Every error toast shows `request_id` as "Reference: …".
- **Never stale money.** The BFF caches nothing. When a refetch fails, the UI keeps the last snapshot but marks it ("Last updated 14:02:31. Updates paused.") and **disables approve, retry and calculate** until a fresh fetch succeeds.
- **Formatting:**
  - `formatCents` uses integer division into dollars and cents, then `Intl.NumberFormat("en-US", { style: "currency", currency: "USD" })` on the parts;
  - basis points display as `xx.xx%`, hours as `h.hh`;
  - dates are shown as calendar dates with no timezone conversion (period dates are `date` values in the company timezone, plan §2.1), and timestamps in the browser's locale;
  - a null amount (a `pending` payslip) shows as "—".
- **Paging (C8).** Every list is paged with `<CursorPager>`: it keeps a stack of the cursors seen, so "Next" sends `cursor=next_cursor` and "Previous" pops the stack; "Next" is disabled when `next_cursor` is null. Page size is 50 unless stated. The cursor is opaque and kept in component state, not in the URL. Changing a filter resets the stack.
- **User references.** `created_by`, `calculated_by`, `approved_by`, `bank_updated_by` and `actor_id` are user IDs as strings. They show as "you" when equal to `Me.id`; otherwise the `*_name` field where the model has one, else the first 8 characters of the ID.
- **Role gating.** `<RoleGate roles>` hides controls the user can't use, and the BFF still enforces. A page the user can't open at all renders `<Forbidden/>` (status 403 text, a link to the dashboard).

### 6.2 Pages

| Page | Purpose | Data (BFF) | Actions | Roles | Req |
|---|---|---|---|---|---|
| `/login` | Sign in | — | Submit `POST /api/auth/login`, then go to `next` when it's safe, else `/dashboard`. `safeNext()` refuses control characters and backslashes outright (the URL parser strips tab, CR and LF and reads `\\` as `/`, so `/\t/evil` would become `//evil`), resolves `new URL(next, location.origin)`, requires the same origin, and returns `pathname + search` (C63). 401 → "Email or password is incorrect."; 429 → countdown from `Retry-After`; 503 → "try again". A "Forgot password?" link goes to `/reset` | public | PAY-104 |
| `/accept` | Accept an invite | — | On mount, read `location.hash` (`#token=…`), keep it in component state, and call `history.replaceState(null, "", "/accept")`, so the token leaves the address bar and history. The token is never put in a query string, log or telemetry. Fields: password and confirm (≥ 12). Submit `POST /api/auth/accept`, then `/dashboard`. 400 `invite_invalid` → its message, with no form | public | PAY-103, NFR-07 |
| `/reset` | Request or confirm a reset | — | **No `#token`:** an email form → `POST /api/auth/reset/request` → always "If an account exists for that email, we've sent a reset link." **With `#token`:** read and strip it as on `/accept`; new password and confirm → `POST /api/auth/reset/confirm` → `/login` with the notice "Password changed. Sign in with your new password." | public | PAY-105 |
| `/dashboard` | The landing page after sign-in | `GET /api/auth/me`; `GET /api/pay-runs?status=processing&limit=10`; `GET /api/pay-runs?status=partially_failed&limit=10`; `GET /api/pay-runs?limit=10`; `GET /api/employees?status=active&limit=50` (shows the exact count when `next_cursor` is null, else "50+") | Cards: runs in `processing` (link; "{counts.blocked} blocked" when non-zero), runs `partially_failed` (link, "needs attention"), the latest runs. Quick actions: "New pay run" and "Add employee" (payroll_admin) | any | PAY-303, 413 |
| `/employees` | The employee list | `GET /api/employees?status&cursor&limit=50` | Filter by status (active/inactive). Columns: name, email, pay type, pay basis (annual salary or hourly rate), schedule, bank (`••••<account_last4>`, or "No bank details" when `has_bank_details` is false), status. Cursor pager. "Add employee" (payroll_admin) | any; add: payroll_admin | PAY-202, J2 |
| `/employees/new` | Create an employee | `GET /api/settings` (for the schedule hint) | Form per `EmployeeCreateBody`: pay type switches between the salary and rate fields; inline ABA checksum; account 4–17 digits. Submit → `/employees/[id]`. 409 `employee_email_taken` shown on the email field | payroll_admin (others get `<Forbidden/>`) | PAY-201 |
| `/employees/[id]` | View and edit | `GET /api/employees/[id]` | View for every role. Edit (payroll_admin): the fields as on create. **Bank details are a separate "Change bank details" dialog** that takes a full new routing and account number and never shows the existing ones (only `••••last4`). Deactivate or reactivate with a confirm dialog; when `has_bank_details` is false, the reactivate dialog also requires a new routing and account number and sends them in the same PATCH (PAY-203, PAY-608). Shows `bank_updated_at` and `bank_updated_by` | view any; edit: payroll_admin | PAY-202, 203, ADR 0004 |
| `/settings` | Company settings | `GET /api/settings` | Read-only for non-admins. Admin form: name, tax rate (%), retirement rate (%), timezone (select), biweekly anchor (date; Mondays only), "Require dual approval" switch. Turning it off asks for confirmation: "Runs already calculated keep dual approval." Save → `PUT /api/settings`; 409 `anchor_locked` shows its message on the anchor field. Shows `updated_at` and `updated_by`. A 404 `company_settings_not_found` shows its message instead of the form | view any; edit: admin | PAY-200, ADR 0005 |
| `/users` | Users and invites | `GET /api/users?cursor&limit=50` | Table: name, email, roles, status (pending, active, inactive). Cursor pager. "Invite user" dialog (email, name, role checkboxes; at least one) → `POST /api/users/invite`. Per row: "Edit roles", and "Deactivate" (confirm) on active users → `PATCH /api/users/[id]`, with these controls disabled on the admin's own row (`cannot_modify_self`). **There is no "Reactivate"**: inactive users are read-only (R37, C25). "Resend invite" for pending users → `POST /api/invites/[pending_invite_id]/resend` (R21, C11) | admin | PAY-101, 102, 108 |
| `/pay-runs` | The run list | `GET /api/pay-runs?cursor&limit=50` | Columns: period, schedule, pay date, status badge, net total, payout counts (paid / failed / requested from `counts`). Newest period first; cursor pager. "New pay run" (payroll_admin) | any | PAY-303 |
| `/pay-runs/new` | Create a run | `GET /api/settings` | Schedule select. `period_start` date input with suggestions: monthly → the next first-of-month dates; biweekly → `biweekly_anchor + 14k` dates. The read-only derived `period_end` is shown for information; payroll derives the real value. Submit → `/pay-runs/[id]`. Errors: `pay_run_exists` (links to the existing run when it can be found in the list), `invalid_period_start`, `no_employees`, `employee_in_overlapping_run` (listing the employees) | payroll_admin | PAY-300 |
| `/pay-runs/[id]` | Run detail (§6.3) | `GET /api/pay-runs/[id]`, `/payslips`, `/payouts` | Hours, calculate, delete, approve, retry, resolve | view any; actions by role | PAY-301–304, 400, 401, 413, 500, 501, 502 |
| `/audit` | Audit logs | `GET /api/audit/auth?cursor&limit=50`, `GET /api/audit/payroll?cursor&limit=50` | Two tabs, each with its own cursor pager. Auth tab: time, event, actor and target IDs, `ip_hash` truncated. Payroll tab: time, event, `actor_id`, `entity_type`, `entity_id`. On both tabs `details` shows its key names only, never values | admin | PAY-109, plan §8 |

### 6.3 The run page (`/pay-runs/[id]`)

**Header (`run-header.tsx`).**
- Period, schedule, pay date and status badge (`draft`, `calculated`, `processing`, `paid`, `partially_failed`), each with a text label and icon, not colour alone.
- Totals: gross, tax, retirement, net, and **funding required** (from `totals`; "—" while `draft`). Employee count (`employee_count`).
- Payout counts from `counts` (C9): paid (with "{no_transfer} with no transfer" when non-zero), failed, requested, and within requested, awaiting bank and blocked.
- "Calculated by {calculated_by_name}" and "Approved by {approved_by_name}" (R21), each shown as "you" when the ID equals `Me.id`, with `calculated_at` and `approved_at`. A null name falls back to the short ID (§6.1).
- When `require_dual_approval` is true: a "Dual approval" label (the calculate-time snapshot).

**Actions (`run-actions.tsx`).**

| Action | Shown when | Role | Call |
|---|---|---|---|
| Save hours | `draft` or `calculated`, and the run has hourly payslips | payroll_admin | `PUT /api/pay-runs/[id]/hours` → `Hours`. A calculated run goes back to draft; the UI says so before saving. On success the run and payslips queries are invalidated and refetched (C7). 422 `not_hourly` / `employee_not_in_run` highlight the rows in `details.employee_ids` |
| Calculate | `draft` or `calculated` | payroll_admin | `POST …/calculate`. 422 `hours_missing` highlights the missing rows |
| Delete | `draft` or `calculated` | payroll_admin | Confirm dialog, then `DELETE`, then `/pay-runs` |
| Approve | `calculated` | approver | `approve-button.tsx` (§3.2). The confirm dialog shows the funding total and the employee count. If `require_dual_approval` is true and `calculated_by === Me.id`, a hint above the button says "You calculated this run. With dual approval on, another approver must approve it." The button stays enabled, because payroll also applies the bank-change rule, which the UI can't see. On 403 `approver_must_differ`, an inline `role="alert"` shows the dual-approval message (§3.3); 409 `employee_bank_details_missing` shows the approve message |

**Tabs** (`RunTabs` in `run-detail.tsx`, the WAI-ARIA tabs pattern without Radix: a roving `tabIndex`, ArrowLeft/ArrowRight/Home/End with automatic activation, one `role="tabpanel"` per tab with the inactive one `hidden` and unmounted; the default is `payslips` before approval and `payouts` after):
- **Payslips** (`payslips-tab.tsx`). `GET …/payslips?cursor&limit=50` with a cursor pager (C8). One row per payslip: employee, pay type, hours, gross, tax, retirement, net (amounts "—" while `pending`). Clicking a row opens `payslip-dialog.tsx` (`GET …/payslips/[payslipId]`). The table footer shows the totals from the run summary, never sums computed in the browser.
- **Hours editor** (`hours-editor.tsx`; payroll_admin, `draft`/`calculated` only). It loads **every** payslip by following `next_cursor` (`limit=100`) until it is null (C8), and lists the `hourly` ones with an input each (prefilled from `hours_hundredths`). Save sends one `HoursBody` with the changed rows (max 500 per request; more are sent in sequential requests of 500).
- **Payouts** (`payouts-tab.tsx`, PAY-501). `GET …/payouts?cursor&limit=50` with a cursor pager and a status filter (all, requested, paid, failed). One row per payout: employee, amount (`amount_cents`), bank `••••<account_last4>` of the current attempt, badge, failure reason, attempt number, and a "History" disclosure (`attempt-history.tsx`) listing every attempt with its status, reason, bank reference, `••••account_last4`, `requested_at` and `final_at`. Rows link to their payslip by `payslip_id`.
  - A "Retry" button (`retry-button.tsx`, approver) appears on `failed` rows only. On 403 `approver_must_differ` it shows the retry message (§3.3). The other 409s show their messages.
  - A "Resolve" button (approver) appears on **Blocked** rows only (current attempt `needs_operator`) and opens `resolve-dialog.tsx` (PAY-502, R8; below).

**Resolve dialog (`resolve-dialog.tsx`, PAY-502).**
- Title "Resolve blocked payout"; it shows the employee, amount, attempt number and `••••account_last4`.
- **A warning at the top, before the choices:** "Check the bank first. Look up this transfer with the bank (runbook `PayoutNeedsOperator.md`: `GET /v1/transfers/{key}`) before you choose. Don't mark it rejected if the bank shows it settled."
- **Two radio choices** (no default selected, so the user must choose):
  - "Send again (redrive)": "Use when the bank has no record of the transfer, or it is pending, and the cause is fixed. The same attempt is sent again." → `action: "redrive"`;
  - "Mark rejected": "Use when the bank shows no settled transfer. The payout becomes failed, and Retry becomes available." → `action: "mark_rejected"`.
- **A required note** (`<textarea>`, 1–500 characters, with a live counter), labelled "Note for the audit log (what you checked at the bank)". Only its length is audited.
- The submit button reads "Resolve" and is disabled until a choice and a note are present, and while the mutation is pending. Submit → `POST /api/pay-runs/[id]/payouts/[payoutId]/resolve` with `ResolveBody` (never retried, `retry: 0`).
- **On 200:** close the dialog, toast "Payout resent" or "Payout marked rejected", and invalidate the run and payouts queries. A redrive moves the run back to `processing`, which restarts polling.
- **On 409 `payout_not_blocked`:** show its message in the dialog's `role="alert"` and refetch the payouts.
- **On 409 `resolve_too_early`:** keep the dialog open, show the message with a countdown from `Retry-After`, and disable "Mark rejected" until it reaches 0. "Redrive" stays available, because it has no wait. 422 errors show inline on the note or the choice. Other errors follow §6.1.
- Built on Radix `AlertDialog`: focus starts on the warning heading, is trapped, and returns to the Resolve button on close.

**Payout badge (`lib/payout-badge.ts`).** The input is the payout plus its current attempt (`attempts` where `attempt === current_attempt`).

| Payout status | Current attempt | `no_transfer` | Badge | Notes |
|---|---|---|---|---|
| `paid` | any | true | "Paid (no transfer)" | net = 0 |
| `paid` | `settled` | false | "Paid" | bank reference in history |
| `failed` | `rejected` | — | "Failed" + reason | Retry shown (approver) |
| `requested` | `requested` | — | "Processing" | |
| `requested` | `indeterminate` | — | **"Awaiting bank"** | PAY-413 |
| `requested` | `needs_operator` | — | **"Blocked"** | PAY-413, PAY-409. Tooltip: "An operator must resolve this payout. It can't be retried." Resolve shown (approver; PAY-502) |

Failure reasons display as: `account_closed` → "Account closed", `insufficient_funds_at_bank` → "Insufficient funds at the bank", `operator_rejected` → "Rejected by an operator". Anything else shows its code.

**Polling (PAY-413).**
- `useQuery(["pay-run", id], …, { refetchInterval: (q) => q.state.data?.status === "processing" ? 2000 : false, refetchIntervalInBackground: false })`. This one endpoint carries every number the header and banner need, including `counts.awaiting_bank` and `counts.blocked` (R21, C9).
- The **payouts query** (the visible page only) uses the same interval, keyed off the run query's status, **only while the Payouts tab is open**, for the per-row badges.
- **Polling stops** as soon as the status is `paid` or `partially_failed`. The run query and, if mounted, the payouts query then refetch once, so the final state is complete.
- **Run-level banner while `processing`** (all from `counts`):
  - "Paying employees: {paid} of {paid + failed + requested} paid";
  - plus "{awaiting_bank} awaiting bank", when non-zero;
  - plus "{blocked} blocked: an operator must resolve these", when non-zero, with a link that opens the Payouts tab filtered to `requested`.
- A retry or a redrive moves the run back to `processing`, which restarts polling.

**Accessibility notes (PAY-NFR-14):**
- **Status changes** are announced through a polite `aria-live` region (`live-region.tsx`). It speaks only when the status or a count changes (for example "Run paid: 498 paid, 2 failed"), never on every poll.
- **Badges** carry text; colour is never the only signal. Contrast is at least 4.5:1 in the theme tokens.
- **Errors.** The dual-approval and other action errors render in `role="alert"`, linked to the button with `aria-describedby`. Form fields use `aria-invalid` and `aria-describedby` for their errors. After a failed submit, focus moves to the first invalid field.
- **Dialogs** (Radix) trap focus and return it to the trigger. Destructive confirms and the resolve dialog use `AlertDialog`; the resolve choices are a labelled radio group, and the note's counter is announced politely.
- **Tables** use `<th scope="col">` and a `<caption>` (visually hidden where needed). The hours inputs have labels like "Hours for {employee}".
- **Tabs** follow the WAI-ARIA tabs pattern (Radix), with arrow keys.
- **Navigation:** every page has a unique `<title>` and one `<h1>`; a skip link goes to `<main>`.
- **Motion:** spinners respect `prefers-reduced-motion`. Polling never moves focus.

## 7. Observability

### 7.1 Logs (`lib/logger.ts`, pino)

- **Output:** JSON lines to stdout. When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, a second pino stream re-emits each (already redacted) line through the OTel logs API (`logs.getLogger("web").emit`, in the active context), and the `BatchLogRecordProcessor` that `lib/otel.ts` registers exports it to the Collector and Loki. There's no `pino-opentelemetry-transport` (C62). Level `LOG_LEVEL`, read case-insensitively (the docs write it in uppercase; C24) and lower-cased for pino.
- **Base fields:** `service: "web"`, `env`, `version` (`APP_VERSION`).
- **Added per line:**
  - `ts` (ISO), `level` (label), `event`, `msg`, and `route` (the route template, a child binding in `route()`);
  - `trace_id` and `span_id` from `trace.getActiveSpan()` (a mixin);
  - `request_id` from the AsyncLocalStorage (the same mixin).

  These match the Python field names (`observability.md` §1).
- **Redaction** (PAY-NFR-07). A custom `formatters.log` walks the object to depth 6 and masks with `"[REDACTED]"` any key whose lower-cased name **contains** one of:
  - the shared deny-list: `password`, `token`, `authorization`, `cookie`, `account`, `routing`, `api_key`;
  - web additions: `set-cookie`, `secret`, `private_key`, `session`, `sid`, `idempotency`, `email`, `name`, `note`, `cents`, `salary`, `rate`.

  - **Exact allow-list:** `user_id_hash`, `ip_hash`, `note_len` and `message_len` contain a denied word but hold a derived value, so they stay readable. These are exact names, not suffixes: `password_hash` or `token_hash` are still masked (C63).

  The custom formatter is used because pino's path-based `redact` can't match keys at any depth. The `email` and `name` additions keep personal data and amounts out of logs, which carry IDs and counts only (plan §11). `account` also masks `account_last4`, which is acceptable.
- **User-entered text** (client error messages) is logged as its length outside dev.
- **User IDs** in logs are `user_id_hash` = the first 16 hex chars of `HMAC(SESSION_SECRET, user_id)`. Raw user IDs appear only on spans (a bounded need for debugging).

| Event | Level | Fields | When | Req |
|---|---|---|---|---|
| `http.request` | info (debug for `/api/livez`, `/api/readyz`) | `method`, `route` (template), `status`, `duration_ms`, `user_id_hash?`, `code?` | Every BFF request, once | PAY-604, NFR-09 |
| `bff.upstream_error` | warning | `target`, `operation` (`"<METHOD> <path>"` with UUIDs replaced by `{id}`), `status?`, `code?`, `error_kind` (timeout, connect, breaker_open, http), `attempts` | Final failure of an upstream call | PAY-602 |
| `bff.upstream_retry` | warning | `target`, `operation`, `attempt`, `delay_ms` | Before each GET retry | plan §4 |
| `bff.upstream_auth_failed` | error | `target`, `operation` (the BFF route template, or `users_me` for the re-check) | Upstream 401 `invalid_token` (a route call, or the §4.3 re-check) | PAY-600 |
| `bff.unmapped_error` | warning | `target`, `status`, `code` | An upstream code not in the catalog | PAY-602 |
| `bff.response_invalid` | error | `target`, `operation` (the BFF route template, or `users_me`), `issues` (paths only) | A response fails its Zod schema | PAY-202, NFR-08 |
| `external.breaker.opened` / `external.breaker.closed` | warning / info | `target` | Breaker transition | plan §12.1 |
| `auth.session.created` | info | `user_id_hash`, `via` (login, accept) | §4.1 | PAY-106 |
| `auth.session.revoked` | info | `user_id_hash`, `reason`, `count` | §4.4 | PAY-107 |
| `auth.session.recheck_deferred` | warning | `user_id_hash`, `age_s` | §4.3 step 5, identity down | PAY-107 |
| `auth.session.revoke_failed` | error | `user_id_hash`, `reason` | Redis error while revoking | PAY-107 |
| `session.backend_error` | error | `op` | Redis error in a session operation | PAY-603 |
| `ratelimit.limited` | info | `scope`, `route` | 429 | PAY-603 |
| `ratelimit.backend_error` | warning | `scope`, `route`, `mode` (fail_open, fail_closed) | Redis error in the limiter | PAY-603 |
| `security.bad_origin` | warning | `route`, `origin_host` | 403 `bad_origin` | PAY-604 |
| `security.payload_too_large` | warning | `route`, `bytes` | 413 | PAY-604 |
| `security.client_ip_unknown` | warning | `xff_entries` (count) | At most once per minute | PAY-603 |
| `web.vitals` | info | `vital` (the metric name), `value`, `rating`, `page` (the page template). Not `name`/`route`: `name` is a denied key and `route` is the BFF route binding | Telemetry | §7.5 |
| `web.client_error` | warning | `page`, `message` (dev) or `message_len` (uat, prod) | Telemetry | §7.5, NFR-07 |
| `web.server_error` | error | `route`, `digest`, `exc` | `onRequestError` | PAY-602 |
| `bff.payout_resolve` | info | `action`, `note_len`, `status` | §3.2 resolve route, after payroll answers (never the note text) | PAY-502 |
| `bff.unhandled_error` | error | `exc` (the stack) | A non-`BffError` thrown in a route (answered 500 `internal_error`) | PAY-602 |
| `config.invalid` | error | `setting` (name only, never the value) | Startup check fails (the process then exits 2; C29) | PAY-605 |

### 7.2 Metrics (`lib/metrics.ts`, meter `web`)

`auth_signins_total` belongs to identity and isn't emitted here. HTTP client and server auto-metrics aren't produced by `@vercel/otel`; the BFF journey SLI is the explicit histogram below (plan §11).

| Metric | Type | Labels | When | Req |
|---|---|---|---|---|
| `bff_request_duration_seconds` | histogram; buckets 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2.5, 5, 10 s | `route` (template from §3.2; bounded set), `status` (HTTP code as a string) | Every BFF request, in `route()` step 10. It feeds `sli:bff_errors:ratio_rate*` and the p95 SLO | PAY-NFR-04, NFR-05 |
| `bff_upstream_requests_total` | counter | `target` (identity, payroll), `outcome` (ok, http_4xx, http_5xx, timeout, connect_error, breaker_open) | Each upstream attempt | plan §4 |
| `circuit_breaker_state` | gauge (0 closed, 1 half-open, 2 open) | `target` | On transition | plan §4, §12.1 |
| `rate_limited_total` | counter | `scope` (ip, session, telemetry) | Each 429 from the BFF limiter | PAY-603 |
| `rate_limiter_errors_total` | counter | `component="web"` | Each limiter Redis error | PAY-603 |
| `bff_session_backend_errors_total` | counter | — | Each session-store Redis error | PAY-603 |
| `bff_sessions_created_total` | counter | `via` (login, accept) | §4.1 | PAY-106 |
| `bff_sessions_revoked_total` | counter | `reason` | §4.4 | PAY-107 |

**SLI exclusion.** The SLI recording rules drop the health and telemetry routes with `route!~"/api/livez|/api/readyz|/api/telemetry"` (C4). The histogram still records them, so they appear in dashboards.

**Export.** The metrics leave through an OTLP HTTP metric exporter to the Collector. With the Collector's Prometheus exporter they appear with `service_name="web"`.
- The histogram instrument is created as name `bff_request_duration`, unit `s`, so the exporter's unit suffix produces `bff_request_duration_seconds` exactly. `lib/metrics.test.ts` asserts the exported name.
- Counters likewise drop `_total` from the instrument name, so the exporter's suffix produces the names above.

### 7.3 Request and trace IDs

- **`x-request-id`:**
  - generated in `proxy.ts` (UUIDv7, unless a well-formed one arrived);
  - passed to handlers as a request header;
  - sent to services as `X-Request-ID`;
  - returned on every BFF response and in `error.request_id`.
- **`x-trace-id`:** the active span's trace ID (32 hex), set in `route()` step 10 on every BFF response, error responses included (PAY-604). The web sends every span (`parentbased_always_on`; C23). In prod the Collector's `tail_sampling` keeps all errors, traces slower than 300 ms and 10% of the rest (R27), so the ID is always present but a fast, successful trace may not be stored. The test stack keeps every trace, which the e2e trace test relies on.

### 7.4 Tracing (`src/instrumentation.ts`, `lib/otel.ts`)

`register()` runs on the Node.js runtime only, and not during `next build` (`NEXT_PHASE !== "phase-production-build"`, which has no secrets). It imports `lib/otel.ts`, whose `startNode()`:
1. runs `loadConfig()` (a failed check logs `config.invalid` and exits 2; C29);
2. calls `registerOTel({ serviceName: "web", attributes: { "service.version", "deployment.environment.name" }, instrumentations: ["fetch", new IORedisInstrumentation({ dbStatementSerializer: (cmd) => cmd })], instrumentationConfig: { fetch: { propagateContextUrls: [originRegex(IDENTITY_BASE_URL), originRegex(PAYROLL_BASE_URL)] } }, metricReaders, logRecordProcessors })`. With `OTEL_EXPORTER_OTLP_ENDPOINT` set, `metricReaders` is one `PeriodicExportingMetricReader` (OTLP HTTP, 15 s) and `logRecordProcessors` one `BatchLogRecordProcessor` (OTLP HTTP); otherwise both are empty;
3. connects Redis (`redis()`), so the first request doesn't race the connection.

`onRequestError` (from `lib/on-request-error.ts`) logs `web.server_error`. OTel packages are SDK 2.12 / 0.223, matching the `@vercel/otel` 2.x peer ranges (C62). `tests/lib/otel.test.ts` checks the wiring.

- **`propagateContextUrls`** is built from `IDENTITY_BASE_URL` and `PAYROLL_BASE_URL` themselves (`originRegex` escapes the origin and anchors it as `^<origin>/`). So the test override that routes payroll through `toxiproxy:18001` still propagates `traceparent`. A missing host would silently break the J4 trace (PAY-NFR-09); `trace.spec.ts` catches that.
- **Endpoint:** `OTEL_EXPORTER_OTLP_ENDPOINT` → the Collector on port 4318 (HTTP/protobuf). Sampler: `OTEL_TRACES_SAMPLER=parentbased_always_on` in every environment; there is no `OTEL_TRACES_SAMPLER_ARG` (C23). Prod sampling is the Collector's job (R27).
- **Instruments are created lazily** against the current global meter provider (`lib/metrics.ts`): the metrics API has no proxy meter, so instruments made before `register()` would stay no-ops.
- **Manual spans:** `BFF <METHOD> <route template>` around each `route()` call (attributes `http.route`, `http.request.method`, `http.response.status_code`), and `bff.session.require` (attribute `session.recheck: boolean`).
- **Span hygiene:**
  - span attributes carry route templates, never raw query strings;
  - `url.full` on fetch spans to services contains only IDs (no tokens, which are never in URLs: PAY-103);
  - the ioredis serializer drops arguments, so no session key or IP hash appears in spans;
  - the Collector's redaction processor masks `token`-named attributes as defense in depth (plan §5 J1).

### 7.5 `/api/telemetry`

- **Browser side** (`web-vitals.tsx`): `useReportWebVitals((m) => send({type: "vital", name: m.name, value: m.value, rating: m.rating, route}))`. Errors from `window.onerror` and `unhandledrejection` → `send({type: "error", message: String(e.message).slice(0, 500), route})`.
- **Transport:** `send` uses `navigator.sendBeacon("/api/telemetry", new Blob([json], {type: "application/json"}))`, falling back to `fetch(…, {keepalive: true})`.
- **`route`** is a page template: `routeTemplate(location.pathname)` replaces UUIDs with `[id]` (so `/pay-runs/[id]`), never the real path with IDs, the query or the fragment. The BFF accepts only `TelemetryRoute = /^\/[a-z0-9\-/[\]]*$/` (≤ 100 characters), so no free text reaches the logs through it (PAY-NFR-07, C63); the browser sends `/[other]` for anything else.
- **Server side:** auth `optional`; Origin check; 8 KB body; `BFF_TELEMETRY_RATE_LIMIT_IP` (60/min per IP), failing open, and never the per-session limit (C63). It logs `web.vitals` or `web.client_error` and returns 204. No metrics are derived from it in 0.1.0.

## 8. Configuration

All keys are read once by `lib/config.ts` (Zod).
- **Parsing** runs in every environment: a missing or malformed key logs `config.invalid` and exits 2.
- **Startup checks** run in uat and prod and refuse to start, logging `config.invalid` with the setting's name only and exiting 2 (PAY-605, C29, ADR 0008), when:
  - a required secret is missing or still has its dev default;
  - `INTERNAL_JWT_KID` starts with `dev-` or `test-`;
  - `PUBLIC_BASE_URL` isn't `https`;
  - `SESSION_COOKIE_NAME` isn't `__Host-sid`;
  - `SESSION_SECRET` is shorter than 32 bytes;
  - `INTERNAL_JWT_PRIVATE_KEY` doesn't import as Ed25519 PKCS8;
  - `TRUSTED_PROXY_HOPS < 1`: with no proxy hop the BFF would trust a client-supplied `X-Forwarded-For` (§5.2; C62).
- **Secrets** come from `.env` (`scripts/dev-env.sh`) in dev and CI, and from External Secrets in uat and prod.

| Key | Type | Default dev / uat / prod | Notes | Req |
|---|---|---|---|---|
| `ENV` | `dev`\|`uat`\|`prod` | dev / uat / prod | Drives the startup checks, HSTS and CSP dev relaxations | PAY-605 |
| `APP_VERSION` | string | git SHA (build arg) | `service.version`, log `version` | NFR-09 |
| `PORT` | int | 3000 | The port inside the container in every environment. Compose publishes host `127.0.0.1:${WEB_PORT}` (4100) → 3000; Kubernetes probes and the Service target 3000 (C4) | NFR-13 |
| `PUBLIC_BASE_URL` | URL | `http://127.0.0.1:${WEB_PORT}` (`http://127.0.0.1:4100`) / per env (https) / per env (https) | Origin check, open-redirect guard, CSP `upgrade-insecure-requests`. identity's `IDENTITY_PUBLIC_BASE_URL` (the invite and reset link base) is set to the same value (R24, C12) | PAY-604 |
| `IDENTITY_BASE_URL` | URL | `http://identity:8000` (direct in every environment, the test stack included) | Also feeds `propagateContextUrls` | NFR-09 |
| `PAYROLL_BASE_URL` | URL | `http://payroll:8000` (test override: `http://toxiproxy:18001`) | Same | NFR-09 |
| `COMPANY_ID` | uuid | `0193a000-0000-7000-8000-000000000001` (dev, C28) / per env / per env | `cid` on `service:web` tokens (R9) | PAY-600 |
| `INTERNAL_JWT_PRIVATE_KEY` | secret (PKCS8 PEM, Ed25519) | `.env` / External Secrets | Signs BFF tokens | PAY-600 |
| `INTERNAL_JWT_KID` | string | `dev-web-1` / non-`dev-` / non-`dev-` | JWT header `kid` | PAY-600 |
| `REDIS_HOST` | string | `redis` (test override: `toxiproxy`) | C1, C12 | PAY-607 |
| `REDIS_PORT` | int | 6379 (test override: 16379) | C1, C12 | PAY-607 |
| `REDIS_USERNAME` | string | `web` | The ACL user (§2.1) | PAY-607 |
| `REDIS_PASSWORD` | secret | `.env` / External Secrets: the value of `REDIS_WEB_PASSWORD` | Passed to ioredis as `password` | PAY-607 |
| `REDIS_SOCKET_TIMEOUT_MS` | int | 500 | ioredis `commandTimeout` and `connectTimeout`; `enableReadyCheck: false` (C62) | PAY-603 |
| `SESSION_SECRET` | secret (≥ 32 bytes) | `.env` / External Secrets | HMAC for session keys, limiter subjects, `user_id_hash` | PAY-106 |
| `SESSION_COOKIE_NAME` | string | `sid` / `__Host-sid` / `__Host-sid` | | PAY-106 |
| `SESSION_IDLE_TTL_S` | int | 43200 | | PAY-106 |
| `SESSION_ABS_TTL_S` | int | 604800 | | PAY-106 |
| `SESSION_ROLE_RECHECK_S` | int | 60 | | PAY-107 |
| `SESSION_RECHECK_GRACE_S` | int | 300 | Fail-open window while identity is unreachable, then 503 (§4.3; R24, C37) | PAY-107 |
| `SESSION_TOUCH_INTERVAL_S` | int | 60 | Minimum gap between idle-TTL refreshes (R24) | PAY-106 |
| `BFF_RATE_LIMIT_SESSION` | int per min | 120 (test env 1000000) | | PAY-603 |
| `BFF_RATE_LIMIT_IP` | int per min | 300 (test env 1000000) | | PAY-603 |
| `BFF_TELEMETRY_RATE_LIMIT_IP` | int per min | 60 | R24 | §7.5 |
| `BFF_BODY_LIMIT_BYTES` | int | 65536 | R24 | PAY-604 |
| `TRUSTED_PROXY_HOPS` | int ≥ 0 | 0 / 1 / 1 | §5.2 (the R36 spike result, C62). uat and prod refuse values below 1 | PAY-603 |
| `UPSTREAM_READ_TIMEOUT_MS` | int | 3000 | `UPSTREAM_*` keys hold the plan §4 values (R24). One timer per attempt covers connect and read; there's no connect-timeout key (C62) | plan §4 |
| `UPSTREAM_SLOW_READ_TIMEOUT_MS` | int | 10000 | Calculate and approve | plan §4, NFR-05 |
| `UPSTREAM_GET_RETRIES` | int | 2 | | plan §4 |
| `UPSTREAM_GET_DEADLINE_MS` | int | 5000 | No retry is started after this | plan §4 |
| `BREAKER_FAILURES` / `BREAKER_RESET_S` | int | 5 / 30 | Shared names with the services (plan §2.2) | plan §2.2 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL | `http://otel-collector:4318` | HTTP/protobuf | NFR-09 |
| `OTEL_TRACES_SAMPLER` | string | `parentbased_always_on` in every environment; no `OTEL_TRACES_SAMPLER_ARG` (C23) | Prod sampling is the Collector's `tail_sampling` (R27) | NFR-09 |
| `LOG_LEVEL` | string, case-insensitive | `DEBUG` / `INFO` / `INFO` (C24) | | NFR-07 |
| `NEXT_TELEMETRY_DISABLED` | `1` | 1 | No Next.js telemetry egress | — |

**Test-only** (Playwright and k6, from `.env` and the shell; never read by the app):
- `BASE_URL` = `http://127.0.0.1:${WEB_PORT}` (`http://127.0.0.1:4100`; C12);
- `SEED_OWNER_EMAIL`, `SEED_OWNER_PASSWORD`, `SEED_APPROVER_PASSWORD`, `LOAD_USER_EMAIL` (role `payroll_admin` only; R22, C27), `LOAD_USER_PASSWORD`, `SESSION_COOKIE`;
- `SEED_JSON`: the path of the JSON summary that `seed.py --out` wrote (`ci-logs/seed.json`; C33; §9.2, §9.3). Its keys are the platform design's: `mode`, `company_id`, `users.{owner, approver, load}`, `employees`, `runs.{current, draft}`;
- `MAILPIT_URL` (`http://127.0.0.1:4102`), `TEMPO_URL` (`http://127.0.0.1:4130`), `LOKI_URL` (`http://127.0.0.1:4131`), `TOXIPROXY_URL` (`http://127.0.0.1:4134`);
- `IDENTITY_DIRECT_URL` (`http://127.0.0.1:${IDENTITY_PORT}`, 4110) for `role-change.spec.ts`;
- **`PAYROLL_PORT`** (4111), giving `http://127.0.0.1:${PAYROLL_PORT}` for the blocked case in `recover.spec.ts` (C44);
- `INTERNAL_JWT_PRIVATE_KEY`, `INTERNAL_JWT_KID` and **`PAYOUT_WORKER_JWT_PRIVATE_KEY`** (with its kid), read by `scripts/mint_token.py` when the e2e helper runs it (C35, C44).

## 9. Tests

The web gates are ESLint, Prettier, `tsc --noEmit` and Vitest coverage ≥ 80% lines (PAY-NFR-12). Every test cites its requirement ID in a comment.

### 9.1 Vitest (`apps/web/tests/`)

- **Unit tests** have no network: MSW (`setupServer`) mocks identity and payroll at `IDENTITY_BASE_URL` and `PAYROLL_BASE_URL`.
- **Redis:** a small fake implementing the `RedisLike` port (`get/set/hset/hgetall/sadd/smembers/del/incr/expire/pexpire/multi`), with a fault switch.
- **Clock:** fake timers (`vi.useFakeTimers()`).
- **Route handlers** are tested as functions: import `{ POST }` from the route file and call it with a `NextRequest` built by `tests/helpers/request.ts` (cookie, Origin, body, params Promise). `next/headers` `cookies()` is mocked from the request.
- **Integration files** (`*.int.test.ts`) use Testcontainers `redis:7-alpine` and run in the same CI job (`vitest --project integration`). **Deferred (C63):** S7 ships none; `vitest.config.ts` already excludes the pattern. `session.int.test.ts` (PAY-106 TTLs, the `web` ACL `NOPERM` checks for PAY-607) lands when the stack's Redis exists. **F5:** the rate-limit row is covered by `tests/api/rate-limits.routes.test.ts` at the **production values** (the 121st request in a session and the 301st from one IP are 429 with `Retry-After`), with two module graphs ("replicas") sharing one `FakeRedis` store, so they share counts. The Testcontainers variant needs the `testcontainers` dev dependency, which isn't in the workspace (a lock-file change for the lead).
- **F5 file layout.** `tests/api/{auth-flows,users,settings,employees,pay-runs-f5,payouts,resolve,audit,rate-limits}.route(s).test.ts`; `tests/schemas/schemas-f5.test.ts` (the new `expectTypeOf` checks; `toExtend` where the service type allows `| null`); `tests/lib/money.test.ts` (parsers and ABA); `tests/components/{auth-forms,employee-form,settings-form,users-page,audit-page,dashboard,role-gate,hours-editor,payouts-tab,resolve-dialog,run-actions}.test.tsx` (the retry-button rows are in `payouts-tab.test.tsx`; accept, reset and the public pages in `auth-forms.test.tsx`); `tests/app/page-gates.test.tsx` covers every `(app)` page, including the F5 ones and the malformed-`[id]` 404.
- **S7 file layout.** Several rows below are combined in fewer files: `tests/lib/security.test.ts` (client IP, rate limit, Origin, body, CSP), `tests/lib/observability.test.ts` (logger, redaction, metrics, `originRegex`), `tests/lib/otel.test.ts` (the `registerOTel` wiring), `tests/lib/shared.test.ts` (money, payout badge), `tests/api/infra.routes.test.ts` (health, fallback, standard headers, pipeline limits, telemetry), `tests/app/page-gates.test.tsx` (every `(app)` page and the layout call `requireSessionForPage`; PAY-601) and `tests/components/forms.test.tsx` (login, run create, list, telemetry client).

| Test file | Covers | Req |
|---|---|---|
| `tests/lib/session.test.ts` | Create from a `SessionUser` (fields including `name` and `cid = company_id`, `HMAC` key, index `SADD`, TTL = idle); rotation deletes the old session at sign-in; touch only after 60 s; `PEXPIRE` = `min(idle, abs remaining)`; absolute expiry → null and deleted; sign-out `DEL` + `SREM`; `revokeUserSessions` deletes all and the index; a corrupt record → deleted | PAY-106 |
| `tests/lib/require-session.test.ts` | No cookie → 401; unknown → 401 and cookie cleared; Redis error → 503 (never allow); re-check skipped < 60 s and called at ≥ 60 s (fake timers); `roles_version` mismatch → all sessions revoked + 401; status inactive → 401; identity 404 → 401; same version → `roles_checked_at`, `roles` and `name` updated (C10); identity down (503 or 429) within `SESSION_RECHECK_GRACE_S` → allowed + `recheck_deferred`; at 300 s past the last check → 503 (C37); identity 500 → 503 with no grace; identity 401 `invalid_token` → 503 + `bff.upstream_auth_failed`, session kept (C63); a page re-check sends `X-Client-IP`; single-flight (5 parallel calls → 1 `/me`); roles mismatch → 403 with the session kept | PAY-106, 107, 601, 603 |
| `tests/lib/internal-jwt.test.ts` | Header `alg=EdDSA`, `kid`; claims `iss=web`, `aud` per target, `sub`, `roles`, `cid`, `sid` and `name` (user tokens only; R9, C10), `exp − iat = 60`, unique `jti`; verifies with the public key via `jose.jwtVerify`; public flow `sub=service:web`, `roles=[]`, `cid=COMPANY_ID`, and no `name` or `sid` claim; a missing key throws only on first use (build-safe) | PAY-600 |
| `tests/lib/api/resilient-fetch.test.ts` | Per-attempt timeout → 503; 10 s for calculate and approve; GET retried twice on 429/502/503/504 and connect errors, not on 500/4xx/read timeout; POST, PUT, PATCH and DELETE never retried; `Retry-After` ≤ 1 s honoured, > 1 s not retried; deadline 5 s; `X-Request-ID` and `Authorization` sent; metrics outcome labels | plan §4 |
| `tests/lib/api/breaker.test.ts` | Opens after 5 consecutive failures; fails fast with no fetch while open; half-open after 30 s lets one probe through; success closes, failure re-opens; an aborted probe frees the slot (`release()`, C63); 4xx and 500 don't count; `circuit_breaker_state` values | plan §4 |
| `tests/lib/errors.test.ts` | One case per §3.3 row, including the C15 additions (`employee_not_in_run`, `idempotency_key_required`, `company_settings_not_found`, `payslip_not_found`, `not_found`, `method_not_allowed`, `bad_request`, `payout_not_blocked`, `anchor_locked`): status, code and message; context-specific `approver_must_differ` and `employee_bank_details_missing` messages; unknown 4xx → `request_failed`; unknown 5xx → 500; `invalid_token` → 500 + `bff.upstream_auth_failed`; `validation_error` details re-parsed to `[{field, issue}]` with extra keys (for example `input`) stripped (C14); `details.employee_ids` forwarded only for `not_hourly`, `employee_not_in_run`, `employee_in_overlapping_run` and `hours_missing`; no upstream message leaks | PAY-602 |
| `tests/lib/rate-limit.test.ts` | Sliding-window estimate across a window boundary; deny at limit + 1 with `Retry-After`; subjects are hashed (no raw IP in keys); fail-closed vs fail-open per route mode; `rate_limiter_errors_total` | PAY-603 |
| `tests/lib/rate-limit.int.test.ts` | Real Redis: **two limiter instances share counts**; **production values** 120/min per session and 300/min per IP limit (the 121st and 301st requests are 429) | PAY-603 |
| `tests/lib/session.int.test.ts` | Real Redis: TTLs as set (`PTTL`), absolute cap, revocation across "two replicas"; the `web` ACL user can't `XADD stream:payout.requested` or `GET identity:*` (NOPERM) | PAY-106, 607 |
| `tests/lib/client-ip.test.ts` | hops 0/1/2 with 1–4 XFF entries; too few entries → `null`; invalid IP → `null`; never the left-most value when hops < n−1; with `null`, the identity client sends **no** `X-Client-IP` header (C16) and the limiter uses the shared `unknown` subject | PAY-603 |
| `tests/lib/origin.test.ts` | Same origin passes; other scheme, host or port, `null`, or missing → 403 `bad_origin`; GET not checked | PAY-604 |
| `tests/lib/body.test.ts` | Content-Length > 64 KB → 413 without reading; a chunked body over the limit → 413; bad JSON → 400; wrong content type → 422; schema errors → 422 with `[{field, issue}]` and no values | PAY-604, 602 |
| `tests/lib/csp.test.ts`, `tests/proxy.test.ts` | CSP string per env (nonce, `strict-dynamic`, `unsafe-eval` dev only); unique nonce per request; `x-nonce` and CSP on the request headers; open pages and every `/api/*` path pass without a cookie (the route handler answers 401; C63); page → 307 `/login?next=`; with a cookie → passes **with no Redis access** (the redis module is not imported by `proxy.ts`) | PAY-604, 106, 601 |
| `tests/lib/logger.test.ts` | Inside a span: JSON line with `trace_id`, `span_id`, `request_id`, `event`; deep redaction of every deny-list key (nested, mixed case); a token, cookie and JWT set during a full route call never appear in captured output | PAY-NFR-07, NFR-09 |
| `tests/lib/metrics.test.ts` | `InMemoryMetricReader`: `bff_request_duration_seconds{route,status}` recorded once per request with the template route; failure paths increment `rate_limited_total{scope}`, `rate_limiter_errors_total{component="web"}`, `bff_upstream_requests_total{outcome}`; the exported Prometheus name is exact | PAY-NFR-04, 05, 10 |
| `tests/lib/config.test.ts` | Startup checks: each refused setting in uat/prod logs `config.invalid` with its name only (not the value) and exits **2** (C29); a missing key exits 2 in every env; dev defaults accepted in dev; the C12 Redis keys are required and `REDIS_URL` isn't read; `LOG_LEVEL` accepts `INFO` and `info` (C24) | PAY-605 |
| `tests/lib/money.test.ts` | String → cents, percent → bps, hours → hundredths: boundaries, no float (property test with `fast-check` against BigInt arithmetic); `formatCents` | ADR 0006 |
| `tests/lib/payout-badge.test.ts` | Every row of the §6.3 badge table | PAY-413, 501 |
| `tests/schemas/*.test.ts` | Valid and invalid fixtures per request schema (§2.3) and response schema (§2.4). Response fixtures are copied from the service Out models (C5): `Employee` with `has_bank_details`, `bank_updated_by` and a null `account_last4`; `Payslip` pending with null amounts and no `schedule`; `Payout` and `Attempt` with every `PayoutOut`/`AttemptOut` field; `PayRun` with all six `counts` and the `*_name` fields; `User` with `pending_invite_id`; `SessionUser` → `Me` mapping (`id = user_id`; C6). `Employee`, `Payout` and `Attempt` `.strict()` reject `bank_account`/`routing`; `UserPatchBody` rejects `status: "active"` (C25); `expectTypeOf` against `@payrun/api-client` types | PAY-201, 202, 303, 501, 602 |
| `tests/api/auth.routes.test.ts` | login: Origin, fail-closed limiter on Redis error, `service:web` token with `aud=identity` and no `name`/`sid`, `X-Client-IP` sent when known and omitted when unknown, `SessionUser` parsed and returned as `{user: Me}`, session created (with `name`) and rotated, cookie attributes per env, 401/429 mapping with `Retry-After`; logout; accept (`invite_invalid`, `password_too_weak`); reset request always 202; reset confirm deletes every session of `user_id` at once (R19); `/me` re-check returns `Me` | PAY-103–107 |
| `tests/api/users.routes.test.ts` | Admin only (403 for others); list passes `cursor`/`limit` and returns `Page<User>` with `pending_invite_id`; invite 201/409/422; PATCH revokes the target's sessions on roles or status; PATCH `status: "active"` → 422 (C25); `cannot_modify_self`, `last_admin`, `user_not_found`; resend 200 `PendingInvite`, `invite_not_pending`, `invite_not_found` | PAY-101, 102, 107, 108 |
| `tests/api/settings.routes.test.ts`, `employees.routes.test.ts` | Role matrix (GET any, writes admin/payroll_admin); `aud=payroll` with the `name` claim; validation; settings `anchor_locked`, `company_settings_not_found`; `employee_email_taken`, `employee_not_found`; list pass-through of `status`, `cursor`, `limit`; a response containing `bank_account` → 500 `internal_error` + `bff.response_invalid` | PAY-200–203, 601 |
| `tests/api/pay-runs.routes.test.ts` | Create/hours/calculate/delete role and error mapping (`pay_run_exists`, `invalid_period_start`, `no_employees`, `employee_in_overlapping_run` details, `pay_run_locked`, `not_hourly` and `employee_not_in_run` with `employee_ids`, `hours_missing`, `pay_run_not_found`); hours returns `Hours` (C7); list passes `status`/`cursor`/`limit`; calculate uses the 10 s timeout; approve: approver only, **missing `Idempotency-Key` → 400 `idempotency_key_required`, a non-UUID key → 422 `validation_error`** (C34), key forwarded unchanged, never retried, `pay_run_not_approvable`, `approver_must_differ` approve message, `employee_bank_details_missing` approve message, `idempotency_key_reused` | PAY-300–304, 400, 401 |
| `tests/api/payouts.routes.test.ts` | Payslips and payouts: `cursor`, `limit` (and payout `status`) passed through, `Page<T>` returned (C8); retry: approver only, never retried, `payout_not_retryable`, `retry_limit_reached`, `employee_bank_details_missing`, `approver_must_differ` retry message | PAY-500, 501 |
| `tests/api/resolve.route.test.ts` | Approver only (403 for payroll_admin-only); Origin; `ResolveBody` validation (unknown action, empty note, 501-character note → 422 with no value echoed); `{action, note}` forwarded unchanged with `aud=payroll`; never retried; 200 `Payout`; 409 `payout_not_blocked`; 404 `pay_run_not_found` / `payout_not_found`; the note text never appears in captured logs (`bff.payout_resolve` has `note_len`) | PAY-502 |
| `tests/api/audit.routes.test.ts`, `telemetry.route.test.ts`, `health.routes.test.ts` | Admin-only audit with `Page<AuthAuditEvent>` and `Page<PayrollAuditEvent>` (`actor_id`, `details`); telemetry 204, 8 KB limit, own rate limit, logs `web.vitals`/`web.client_error` (length only outside dev); `/api/livez` 200 with Redis down; `/api/readyz` 503 when Redis is down | PAY-109, NFR-07, PAY-605 |
| `tests/api/fallback.routes.test.ts` | Unknown `/api/*` path → 404 `not_found`; an unexported method on a known route → 405 `method_not_allowed` with `Allow`; both carry the standard headers | PAY-602, 604 |
| `tests/api/headers.test.ts` | Every BFF response (success and error, 401 included) has `x-request-id`, `x-trace-id` and `Cache-Control: no-store` | PAY-604 |
| `tests/components/run-page.test.tsx` | MSW on `/api/*`: polls the run every 2 s while `processing` (fake timers), polls payouts only while the Payouts tab is open, stops at `paid`/`partially_failed`, restarts after a retry or redrive; banner counts from `counts.awaiting_bank`/`counts.blocked` with no payouts request; "Awaiting bank" and "Blocked" badges; `calculated_by_name`/`approved_by_name` and "you"; the live region announces only on change; actions disabled when a refetch fails | PAY-303, 413 |
| `tests/components/approve-button.test.tsx` | A new UUID per click, the same key within one click's mutation; disabled while pending; the dual-approval message in `role="alert"`; network error → refetch run → success state if `processing` | PAY-400, 401 |
| `tests/components/retry-button.test.tsx`, `payouts-tab.test.tsx` | Retry only on failed rows for approvers; Resolve only on Blocked rows for approvers; retry messages; amount and `••••account_last4` columns; attempt history rows; cursor pager follows `next_cursor` and disables Next on null | PAY-500, 501, 502 |
| `tests/components/resolve-dialog.test.tsx` | The bank-check warning is shown before the choices; no default choice; submit disabled until a choice and a 1–500 character note exist and while pending; sends `ResolveBody`; 409 `payout_not_blocked` in `role="alert"` and a payouts refetch; success invalidates the run and payouts; focus management | PAY-502, NFR-14 |
| `tests/components/accept-form.test.tsx`, `reset-forms.test.tsx`, `login-form.test.tsx` | Token read from the fragment and stripped with `replaceState`; never sent in a URL; 12-character rule; mapped errors; `next` open-redirect guard, including `/\t/evil`, `/\n/evil` and backslashes (C63) | PAY-103–105 |
| `tests/components/employee-form.test.tsx`, `hours-editor.test.tsx`, `settings-form.test.tsx`, `users-page.test.tsx`, `role-gate.test.tsx` | Field validation and inline 422 details; ABA checksum; bank fields never prefilled; reactivating an employee without bank details requires new ones; the hours editor follows `next_cursor` across 3 pages before showing rows, then refetches the run after save (C7, C8); `anchor_locked` on the anchor field; the users page shows Deactivate but **no Reactivate** (C25) and uses `pending_invite_id` for Resend; role-gated controls hidden | PAY-200–203, 301, 108, 601 |

### 9.2 Playwright (`apps/web/e2e/`)

- **Stack:** `scripts/stack.sh up` (profiles `core,obs,test` with `compose.test.yaml`), then `scripts/seed.py` (owner, `approver@payrun.example.com`, `LOAD_USER_EMAIL` as `payroll_admin`, tax 2000 bps and retirement 500 bps, 10 employees including one account ending `0000`; R22, C27). In full mode it also sets tax 2000 bps and retirement 500 bps; until task F3 the e2e job runs `seed.py --skeleton` (5 salaried monthly employees, no settings PUT; C33). Its JSON summary is written to `ci-logs/seed.json`, passed as `SEED_JSON`.
- **Projects:** one Chromium project; `baseURL = BASE_URL` (`http://127.0.0.1:4100`; never `localhost`, C12).
- **Global setup** (`e2e/global-setup.ts`; part of PAY-609's check with `tests/platform/test_seed.py`, C45): reads `SEED_JSON` and validates it with a Zod schema (`mode`, `company_id` = `COMPANY_ID`, `users.owner`/`approver`/`load` UUIDs, `employees` ≥ 5, `runs.current`/`draft` UUIDs); waits for `GET /api/readyz` = 200; signs in once as the owner and as `approver@payrun.example.com` and stores each `storageState`. Any failure stops the run before the first spec.
- **Helpers:**
  - `e2e/fixtures.ts`:
    - `asOwner` and `asApprover` page fixtures (separate browser contexts, using the `storageState` files written by the global setup);
    - a console listener that **fails the test on any CSP violation** (`securitypolicyviolation` event or a console message containing "Content Security Policy"; ADR 0009);
    - `apiFetch(page, path, init)` runs `fetch` inside the page, so cookies and Origin behave as in the browser (`testing.md` §6).
  - `e2e/helpers/mailpit.ts`: `latestToken(email, kind: "accept" | "reset")`:
    1. Poll `GET {MAILPIT_URL}/api/v1/search?query=to:"<email>"` every 250 ms for up to 15 s, newest first.
    2. `GET /api/v1/message/{ID}` and match the `Text` body against `/\/(accept|reset)#token=([A-Za-z0-9_-]+)/`.
    3. Return the token and the full link. Tests open the link exactly as a user would (`page.goto(link)`).

    Each test uses a unique address (`e2e+<uuid>@payrun.example.com`), so there's no Mailpit cleanup and no cross-test interference.
  - `e2e/helpers/mint-token.ts` (test-only): runs the CLI `uv run python scripts/mint_token.py --aud <aud> --sub <sub> [--roles a,b] [--issuer web|payout-worker] [--name NAME]` (C35) and returns the JWT it prints on stdout. The CLI reads the keys from `.env`. It never signs in TypeScript and holds no key itself. Two uses:
    - `role-change.spec.ts`: an `issuer=web`, `aud=identity` admin token to call identity directly on `IDENTITY_DIRECT_URL`;
    - `recover.spec.ts` blocked case: an `issuer=payout-worker`, `sub=service:payout-worker`, `aud=payroll` token (signed with `PAYOUT_WORKER_JWT_PRIVATE_KEY`) to call payroll's internal result endpoint on `http://127.0.0.1:${PAYROLL_PORT}` (C44).
  - `e2e/helpers/toxiproxy.ts`: enable or disable a proxy; re-enables every proxy and resets all toxics in `afterEach`.

| Spec | Scenarios | Req |
|---|---|---|
| `access.spec.ts` | **J1:** the owner invites `e2e+…` with `[payroll_admin]`; the token comes from Mailpit; accept via the link; the URL has no `token` after load (fragment stripped) and no query token ever (request log listener); lands on the dashboard; `/api/auth/me` roles equal exactly `[payroll_admin]`; the nav hides admin items. Sign out → `/login`; the back button doesn't show data. Wrong password → "Email or password is incorrect." Expired or used invite link → `invite_invalid` message. Reset: request → Mailpit `reset` token → new password → old session gets 401 → sign in with the new password. Unauthenticated `/employees` → `/login?next=/employees`; `/api/employees` → 401 `unauthenticated`. The cookie has HttpOnly, SameSite=Lax and Secure | PAY-101, 103, 104, 105, 106 |
| `employees.spec.ts` | **J2:** admin sets the tax and retirement rates; payroll_admin adds a salaried and an hourly employee; the list shows `••••` + last 4 only, and the page HTML and `/api` responses never contain the full account number; edit bank details (dialog empty, not prefilled); deactivate and reactivate the employee (PAY-203; employee reactivation stays in scope); duplicate email → message on the field; an approver-only user sees no "Add employee" and gets `<Forbidden/>` on `/employees/new`; the employee list pages with Next/Previous | PAY-200–203, 601, NFR-08 |
| `pay-run.spec.ts` | **J3:** create a monthly run; the hourly row requires hours (`hours_missing` on calculate); enter hours → calculate → totals shown; for every payslip row gross = net + tax + retirement, and funding = Σ net (read from every page of the payslips table); a duplicate period → `pay_run_exists` message; a biweekly run with a wrong start → `invalid_period_start`; delete a calculated run → the period is free again | PAY-300–304 |
| `approve-pay.spec.ts` | **J4:** dual approval on; the owner calculates; the owner's Approve → the dual-approval message (exact text, §3.3); the approver approves → status `processing` → polling (at most 2 s between `GET /api/pay-runs/{id}` requests, observed with `page.on("request")`) → `partially_failed` (the `0000` account) within 60 s; the failed row shows "Failed: Account closed"; a second approve via `apiFetch` with a new key → 409 `pay_run_not_approvable`; two parallel approves on a fresh run → exactly one 200 | PAY-400, 401, 413, 501 |
| `recover.spec.ts` | **J5:** continues from a `partially_failed` run; the owner (payroll_admin) edits the failed employee's bank details; the owner (also approver) clicks Retry → the retry `approver_must_differ` message; the approver retries → the run returns to `processing`, then `paid`; the history shows attempt 1 rejected and attempt 2 settled; retrying a paid payout via API → 409 `payout_not_retryable`. **Blocked case (PAY-502, R8, C44):** (1) disable the Toxiproxy `bank-stub` proxy so the worker can't finish; (2) the owner calculates and the approver approves a fresh run, and the test reads two payouts A and B and their current attempts from `GET /api/pay-runs/{id}/payouts`; (3) for each, it posts `{attempt, outcome: "poison", reason: "schema_invalid"}` to `POST http://127.0.0.1:${PAYROLL_PORT}/v1/internal/payouts/{payout_id}/result` with a worker token from `mint-token.ts` and `Idempotency-Key: e2e-<uuid>:poison`; (4) both rows show **"Blocked"**, the banner shows "2 blocked", and Retry isn't offered; (5) re-enable the `bank-stub` proxy; on A the approver opens Resolve, sees the bank-check warning, picks "Send again" with a note → A's history shows the same attempt settled; (6) a second resolve on A via `apiFetch` → 409 `payout_not_blocked`; (7) on B, "Mark rejected" first returns 409 `resolve_too_early`: the dialog shows the wait message with the `Retry-After` countdown. The spec then waits with `expect.poll` (timeout 150 s, because `PAYOUT_MARK_REJECTED_MIN_WAIT_S` is 120 s under production timing, C48/C53) and repeats "Mark rejected" → B shows "Failed: Rejected by an operator" with Retry offered; the approver retries B → the run ends `paid` | PAY-500, 501, 502, 401 |
| `role-change.spec.ts` | (a) The admin removes `approver` from user B in `/users`; B's next approve → 401 at once (sessions revoked through `web:user-sess`) → `/login`; B signs in again → the approve button is hidden, and `apiFetch` approve → 403 `forbidden_role`. (b) **Re-check backstop (C45):** with identity reachable, demote user C at time T by calling identity `PATCH /v1/users/{id}` directly (`mint-token.ts`; bypasses the BFF's revocation); C's requests may succeed before T + 60 s, and the first request C sends at or after T + 60 s must get 401. (c) The admin deactivates user D; `/users` shows D as inactive with no Reactivate control (C25) | PAY-107, 108 |
| `errors.spec.ts` | `/no-such-page` → 404 page; a forbidden page; **identity down** (web → identity is a direct connection, so `e2e/helpers/stack.ts` stops the `identity` container with `scripts/stack.sh` and starts it again in `afterAll`) → login shows "temporarily unavailable, try again" and the response is 503 `service_unavailable`, while a signed-in user can still list employees (payroll path); **payroll down** (the Toxiproxy `payroll` proxy disabled) → employees page shows the error banner with a reference; `apiFetch` with an evil `Origin` (via `page.request` with explicit headers) → 403 `bad_origin`; a 70 KB body → 413 `payload_too_large`; response headers on a page and an `/api` route: CSP with a nonce and `strict-dynamic`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, `x-request-id`, `x-trace-id` (no HSTS in dev) | PAY-602, 604, plan §12.1 |
| `trace.spec.ts` | (a) **J4 trace:** the approver approves via `apiFetch` and reads `x-trace-id`; poll `GET {TEMPO_URL}/api/traces/<id>` for up to 30 s until spans exist from `service.name` = `web`, `payroll`, `payout-worker` and `bank-stub`; then Loki `query_range` `{service_name=~".+"} \| trace_id="<id>"` has at least one line each for web, payroll and payout-worker (bank-stub too if it logs inside the trace). (b) **No tokens in telemetry:** after an accept in this spec, Loki `{service_name=~".+"} \|= "<invite token>"` over the last 15 min returns 0 lines, and Tempo TraceQL `{ span.url.full =~ ".*<token>.*" \|\| span.url.query =~ ".*<token>.*" }` returns no traces | PAY-NFR-09, NFR-07 |
| `a11y.spec.ts` | `@axe-core/playwright` with the `wcag2a`/`wcag2aa` tags on `/login`, `/accept` (invalid-token state), `/reset`, `/dashboard`, `/employees`, `/employees/new`, `/settings`, `/users`, `/pay-runs`, `/pay-runs/[id]` (payslips tab), the payouts tab, `/audit`: no serious or critical violations. Keyboard-only path: tab to Approve, open the confirm dialog, Escape returns focus | PAY-NFR-14, NFR-12 |

Nightly runs the suite three times in a row (plan §12). No `waitForTimeout`: every wait polls with a deadline (`expect.poll`, `toPass`).

### 9.3 k6 (`load/`)

- **Shared helper** `load/lib/session.js`: `login(email, password)` posts `/api/auth/login` with `Origin: BASE_URL` and returns `name=value` for `SESSION_COOKIE` (`sid`; `__Host-sid` in prod-like runs).
- **Mutations** send `Origin: BASE_URL` (`http://127.0.0.1:4100`; C12), and approve sends `Idempotency-Key: uuidv4()` (k6 `crypto.randomUUID`).
- **Paging** (`load/lib/pages.js`): `allPages(path, params)` follows `next_cursor` with `limit=100` until it is null and returns every item (C8). Each page request is tagged with the route template.
- **Seed summary** (`load/lib/seed.js`): `const seed = JSON.parse(open(__ENV.SEED_JSON))` in the init context (`k6 run -e SEED_JSON=ci-logs/seed.json …`), so every VU reads `seed.runs.current`, the run `seed.py` created (C33). A missing file or key fails the script at start.
- **Tags:** every request has `name` (the route template) and `kind` (`simple` or `heavy`).
- The test environment's raised `BFF_RATE_LIMIT_*` and login limits apply (plan §12).

| Script | When | Scenarios | Thresholds | Req |
|---|---|---|---|---|
| `load/smoke.js` | PR (`e2e` job) | `smoke`: `constant-vus` 5 VUs × 30 s, as `LOAD_USER_EMAIL`. Per iteration: `GET /api/auth/me`, `GET /api/employees?limit=50`, `GET /api/pay-runs?limit=20`, `GET /api/pay-runs/{id}` and `GET /api/pay-runs/{id}/payslips?limit=50` for `runs.current` from the seed summary (C33), `GET /api/settings`; `sleep(1)` | `http_req_failed` < 1%; `http_req_duration` p(95) < 300 ms | PAY-NFR-05 |
| `load/full.js` | Nightly | `peak`: `ramping-arrival-rate` 0 → 30 req/s over 1 min, held for 10 min (plan §10: peak 20 × 1.5). The mix: 70% reads (as in smoke); 15% `POST /api/employees` then `PATCH` (unique emails); 10% run lifecycle on a scratch schedule (create, hours, calculate, delete) with `kind:heavy` on calculate; 5% `GET /api/pay-runs/{id}/payouts?limit=50` (one page; every 10th of these walks all pages with `allPages`). `soak` (`-e SCENARIO=soak`): `constant-arrival-rate` 5 req/s × 30 min, reads plus 10% writes | `http_req_failed` < 1%; `http_req_duration{kind:simple}` p(95) < 300 ms; `http_req_duration{kind:heavy}` p(95) < 1500 ms. Soak also checks for no upward drift: the p95 of the last 5 min is within 20% of the first 5 min, compared in `handleSummary` | PAY-NFR-04, NFR-05 |
| `load/settlement.js` | Nightly (with `seed.py --employees 500`) | `settlement`: `shared-iterations`, 1 VU, 5 iterations. `setup()` signs in as `LOAD_USER_EMAIL` (preparer; `payroll_admin` only) and `approver@payrun.example.com` (`SEED_APPROVER_PASSWORD`) (R22, C27). Each iteration: find the next unused monthly `period_start` (from `allPages("/api/pay-runs")`); `POST /api/pay-runs`; `allPages("…/payslips")` until `next_cursor` is null (C8); `PUT …/hours` for every hourly payslip, in requests of at most 500 entries; `POST …/calculate` (as the preparer, `kind:heavy`); `POST …/approve` as the approver with a fresh `Idempotency-Key` (`kind:heavy`); then poll `GET /api/pay-runs/{id}` every 2 s until `paid` or `partially_failed` (timeout 180 s), recording the Trend `run_settlement_seconds` (from the approve response to the first final status) | `run_settlement_seconds` p(95) < 60 s; `http_req_duration{name:calculate}` and `{name:approve}` p(95) < 1500 ms; `checks` > 99%. Cross-checked against `payroll_payout_settlement_seconds` in Prometheus by the nightly script | PAY-NFR-05, NFR-06 |

## 10. Traceability

| Requirement | Design section(s) | Tests |
|---|---|---|
| PAY-101 Invite | §3.2 users, §6.2 `/users` | `users.routes.test.ts`, `access.spec.ts` |
| PAY-102 Resend | §2.4 `PendingInvite`, §3.2 invites, §6.2 `/users` | `users.routes.test.ts`, `users-page.test.tsx` |
| PAY-103 Accept | §3.2 auth, §4.1, §6.2 `/accept`, §7.4 span hygiene | `auth.routes.test.ts`, `accept-form.test.tsx`, `access.spec.ts`, `trace.spec.ts` (b) |
| PAY-104 Sign in | §3.2 auth, §3.3, §5.1, §6.2 `/login` | `auth.routes.test.ts`, `login-form.test.tsx`, `access.spec.ts` |
| PAY-105 Reset | §3.2 auth, §4.4, §6.2 `/reset` | `auth.routes.test.ts`, `reset-forms.test.tsx`, `access.spec.ts` |
| PAY-106 Sessions | §2.1, §2.2, §4.1–4.4, §5.5 | `session.test.ts`, `session.int.test.ts`, `require-session.test.ts`, `proxy.test.ts`, `access.spec.ts` |
| PAY-107 Re-check | §4.3 step 5, §4.4 | `require-session.test.ts`, `users.routes.test.ts`, `role-change.spec.ts` |
| PAY-108 User admin | §2.3 `UserPatchBody`, §2.4 `User`, §3.2 users, §6.2 `/users` | `users.routes.test.ts`, `users-page.test.tsx`, `role-change.spec.ts` |
| PAY-109 Auth audit | §3.2 audit, §6.2 `/audit` | `audit.routes.test.ts`, `a11y.spec.ts` |
| PAY-200 Settings | §2.4 `Settings`, §3.2 settings, §3.3 (`anchor_locked`, `company_settings_not_found`), §6.2 `/settings` | `settings.routes.test.ts`, `settings-form.test.tsx`, `employees.spec.ts` |
| PAY-201 Create employee | §2.3, §3.2 employees, §6.2 | `employees.routes.test.ts`, `employee-form.test.tsx`, `employees.spec.ts` |
| PAY-202 List and read employees | §2.4 (`Employee` strict), §3.2, §6.2 | `employees.routes.test.ts`, `schemas/*.test.ts`, `employees.spec.ts` |
| PAY-203 Update employee | §3.2, §6.2 `/employees/[id]` | `employees.routes.test.ts`, `employees.spec.ts`, `recover.spec.ts` |
| PAY-300 Create run | §3.2 pay runs, §3.3, §6.2 `/pay-runs/new` | `pay-runs.routes.test.ts`, `pay-run.spec.ts` |
| PAY-301 Hours | §2.3 `HoursBody`, §2.4 `Hours`, §3.2, §6.3 hours editor | `pay-runs.routes.test.ts`, `hours-editor.test.tsx`, `pay-run.spec.ts` |
| PAY-302 Calculate | §3.2 (10 s), §6.3 | `pay-runs.routes.test.ts`, `pay-run.spec.ts`, `full.js`, `settlement.js` |
| PAY-303 Summary and payslips | §2.4 `PayRun`, `Payslip`, `Page<T>`, §3.2, §6.3 | `pay-runs.routes.test.ts`, `payouts.routes.test.ts`, `run-page.test.tsx`, `pay-run.spec.ts` |
| PAY-304 Delete run | §3.2, §6.3 | `pay-runs.routes.test.ts`, `pay-run.spec.ts` |
| PAY-400 Approve | §2.3 `IdempotencyKeyHeader`, §3.2 (Idempotency-Key), §3.3, §6.3 | `pay-runs.routes.test.ts`, `approve-button.test.tsx`, `approve-pay.spec.ts`, `settlement.js` |
| PAY-401 Dual approval | §3.3 (message), §6.3 | `errors.test.ts`, `approve-button.test.tsx`, `approve-pay.spec.ts`, `recover.spec.ts` |
| PAY-413 Live progress | §6.3 polling and badges, §5.1 budget | `run-page.test.tsx`, `payout-badge.test.ts`, `approve-pay.spec.ts` |
| PAY-500 Retry | §3.2, §3.3, §6.3 | `payouts.routes.test.ts`, `retry-button.test.tsx`, `recover.spec.ts` |
| PAY-501 Payout history | §2.4 (`Payout`, `Attempt` strict), §3.2, §6.3 | `payouts.routes.test.ts`, `payouts-tab.test.tsx`, `approve-pay.spec.ts`, `recover.spec.ts` |
| PAY-502 Operator resolution | §2.3 `ResolveBody`, §3.2 resolve route, §3.3 `payout_not_blocked` and `resolve_too_early`, §6.3 resolve dialog | `resolve.route.test.ts`, `resolve-dialog.test.tsx`, `payouts-tab.test.tsx`, `recover.spec.ts` (blocked case) |
| PAY-600 Internal JWT | §3.4.1, §3.2 (`U`/`S`) | `internal-jwt.test.ts`, `auth.routes.test.ts`, `employees.routes.test.ts` |
| PAY-601 Authorization | §3.1, §4.3, §5.5, §6.1 | `require-session.test.ts`, `proxy.test.ts`, `*.routes.test.ts`, `role-gate.test.tsx`, `employees.spec.ts` |
| PAY-602 Error format | §2.4 `ErrorBody`, `Page<T>`, §3.3, §1.2 fallback routes | `errors.test.ts`, `*.routes.test.ts`, `fallback.routes.test.ts`, `errors.spec.ts` |
| PAY-603 Rate limits | §4.5, §5.1, §5.2 | `rate-limit.test.ts`, `rate-limit.int.test.ts`, `client-ip.test.ts`, `auth.routes.test.ts` |
| PAY-604 BFF protections | §5.3–5.6, §7.3 | `body.test.ts`, `origin.test.ts`, `csp.test.ts`, `proxy.test.ts`, `headers.test.ts`, `errors.spec.ts`, CSP listener in `fixtures.ts` |
| PAY-605 Health and startup (web part) | §1.2 `/api/livez`/`/api/readyz`, §8 startup checks (exit 2) | `config.test.ts`, `health.routes.test.ts` |
| PAY-607 Redis ACLs (web) | §2.1 ACL, §8 Redis keys | `session.int.test.ts` |
| PAY-609 Seed data (web part) | §8 test-only keys, §9.2 global setup, §9.3 seed summary | `e2e/global-setup.ts`, `smoke.js`, `settlement.js` |
| PAY-NFR-04 Availability SLO | §7.2 `bff_request_duration_seconds` | `metrics.test.ts`, `full.js` |
| PAY-NFR-05 Latency SLO | §3.4.2, §7.2 | `smoke.js`, `full.js`, `settlement.js` |
| PAY-NFR-06 Settlement SLO | §9.3 | `settlement.js` |
| PAY-NFR-07 No secrets in telemetry | §6.2 (fragment), §7.1 redaction, §7.4, §7.5 | `logger.test.ts`, `telemetry.route.test.ts`, `trace.spec.ts` (b) |
| PAY-NFR-08 Privacy | §2.4 strict schemas, §5.1 hashed subjects, §7.1 | `schemas/*.test.ts`, `employees.routes.test.ts`, `employees.spec.ts` |
| PAY-NFR-09 Tracing | §7.3, §7.4 (`propagateContextUrls`) | `trace.spec.ts` (a), `headers.test.ts` |
| PAY-NFR-10 Observability assets (web metrics) | §7.2 | `metrics.test.ts` |
| PAY-NFR-12 Gates (web) | §9 | CI `web` and `e2e` jobs |
| PAY-NFR-14 Accessibility | §6.3 notes | `a11y.spec.ts` |

## Change log

- 2026-11-11 (F5, web complete): every remaining BFF route of §3.2 (accept, reset request and confirm, users, invite, user PATCH with revocation, resend, settings, employees, run DELETE, hours, payslip detail, retry, resolve, both audit routes) and every page of §6.2 (`/accept`, `/reset`, `/employees`, `/employees/new`, `/employees/[id]`, `/settings`, `/users`, `/audit`, the dashboard cards, both schedules on `/pay-runs/new`, and on the run page the hours editor, payslip dialog, delete, payouts filter and history, retry and resolve). Recorded as built: no shadcn/Radix or react-hook-form (§1.4: local `Dialog`, `TabList` and `field.tsx` with the same accessibility contract; inline notices instead of toasts); reset confirm returns 204 even when the revoke hits a Redis error (§3.2, as user PATCH); reset request maps identity's empty 202 (C68) to `{}`; `/login?reset=1` shows the "Password changed" notice; the hours editor sends only rows whose parsed value changed; the rate-limit int test is replaced by a production-value pipeline test on the fake store (§9.1). No contract change was needed: every route matches `services/*/openapi.json`. Not forwarded, as §3.3 says: payroll's `pay_run_exists` `details.pay_run_id` (the create page finds the run in the list instead) and the payroll audit filters (`entity_type`, `entity_id`, `event`).
- 2026-11-11: folded C62 (S7 follow-ups) and the web part of C63 into the body: §1 module list, §2.1 ACL, §3.1–§3.4.2 (no connect-timeout key, `release()`, `unsupported_media_type`/`conflict`, `/api/auth/me` 503 while deferred, telemetry outside the session budget), §4.3 (signatures; re-check outcomes per C63), §5.2 (the R36 result and formula), §5.5 (`/api/*` not gated), §5.6 (HSTS in `proxy.ts`), §6.2 (`safeNext`), §6.3 (tabs), §7.1 (OTel logs bridge, exact allow-list, real field names), §7.4, §7.5, §8, §9.1 (int-test deferral and file layout).
- 2026-11-10: incorporated README R-table and C-table decisions (R3, R4, R8, R9, R17, R18, R19, R21, R22, R23, R24, R27, R33, R34, R36, R37; C4, C5, C6, C7, C8, C9, C10, C11, C12, C14, C15, C16, C23, C24, C25, C26, C27, C28, C29, C31, C33, C34, C35, C37, C44, C45).

Resolved after the change log entry:
- **C51:** until F3, `e2e.sh` runs only the `@skeleton`-tagged spec `skeleton.spec.ts` (sign in as the owner, calculate the seeded run, approve as `approver@payrun.example.com`, the run shows `paid`, and the trace assertion) on the `--skeleton` seed. The J1–J5 specs carry the tag `@journeys` and run from F6, on the full seed. Playwright selects them with `--grep`, and `e2e.sh` reads `E2E_SUITE=skeleton|full`.
- **C52:** the retry button sends an `Idempotency-Key` per click, as approve does.