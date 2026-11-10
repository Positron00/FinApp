# Low-level design: web (Next.js pages and BFF)

Source: [architecture plan](../../plan/architecture-plan.md) v1.2 (accepted 2026-11-10), [ADR 0002](../../adr/0002-sign-in-and-roles.md), [ADR 0005](../../adr/0005-four-eyes-approval.md), [ADR 0009](../../adr/0009-csp.md). Requirements: [requirements.md](requirements.md). High-level design: [design-high-level.md](design-high-level.md).

Scope: `apps/web`, the only public entry point. It serves the pages and the BFF (`/api/*`), holds browser sessions in Redis (`web:*`), mints internal JWTs and calls identity and payroll through the clients generated into `packages/api-client`. Business rules stay in the services; the BFF does sessions, authorization at the edge, validation, request shaping and error mapping.

**Stack decisions for this area:**
- **Next.js 16 (App Router), strict TypeScript.** The request interceptor is `src/proxy.ts`. Next 16's proxy runs on the Node.js runtime, but it stays a cookie-presence check plus the CSP nonce and is **not** a security boundary (`architecture.md` §9; CVE-2025-29927). Every route handler and every page calls `requireSession()` itself (PAY-601). No reason was found to stay on 15.
- Tailwind with shadcn/ui (Radix primitives); TanStack Query v5 in the browser; Zod at every boundary.
- **Service clients:** `openapi-typescript` types plus `openapi-fetch`, from `packages/api-client` (`@payrun/api-client`, exports `IdentityPaths` and `PayrollPaths`).
- **Server libraries:** `ioredis` for Redis, `jose` for EdDSA, `pino` for logs, `@vercel/otel` for traces.
- **Pages render dynamically.** The root layout reads the nonce header, so there's no static export (ADR 0009).
- **Server components don't call services for page data.** Each page's server component only gates (session and role), then renders client components. Those components fetch through `/api/*` with TanStack Query, so every data path goes through the same route wrapper, error mapping and metrics.
- **`next.config.ts`:**
  - `serverExternalPackages: ["pino", "pino-opentelemetry-transport", "ioredis"]`;
  - `poweredByHeader: false`;
  - `output: "standalone"`;
  - `headers()` sets the static security headers (§5.6).

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

Every `page.tsx` under `(app)` calls `requireSessionForPage(roles?)` itself. The layout's check isn't enough, because layouts aren't re-rendered on client navigation.

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
| `api/audit/auth/route.ts` | GET |
| `api/audit/payroll/route.ts` | GET |
| `api/telemetry/route.ts` | POST |
| `api/livez/route.ts`, `api/readyz/route.ts` | GET (liveness: no dependencies; readiness: Redis `PING` within `REDIS_SOCKET_TIMEOUT_MS`) |

### 1.3 Server libraries (`src/lib/`)

Every module here starts with `import "server-only"`, except the ones marked *shared*.

| Module | Responsibility |
|---|---|
| `lib/config.ts` | Parses `process.env` with Zod into a frozen `Config` (§8) and runs the web startup checks. Loaded lazily, so `next build` runs without secrets |
| `lib/redis.ts` | The `ioredis` singleton: user `web`, `commandTimeout = REDIS_SOCKET_TIMEOUT_MS`, `enableOfflineQueue: false`, `maxRetriesPerRequest: 1`, `lazyConnect`. With these settings a Redis outage fails fast instead of queueing |
| `lib/session.ts` | The session store: `createSession`, `loadSession`, `touchSession`, `markRolesChecked`, `deleteSession`, `revokeUserSessions`, plus the cookie helpers (§4) |
| `lib/require-session.ts` | `requireSession(roles?)` for route handlers and `requireSessionForPage(roles?)` for pages (§4.3) |
| `lib/internal-jwt.ts` | `internalToken()`: EdDSA minting (§3.4.1) |
| `lib/api/resilient-fetch.ts` | `resilientFetch(target, opts)`: timeouts, GET retries, breaker, metrics, `X-Request-ID` (§3.4.2) |
| `lib/api/breaker.ts` | A per-process circuit breaker per target |
| `lib/api/identity.ts` | `identityClient(ctx)`: an `openapi-fetch` client over `IdentityPaths`, with a middleware that adds `Authorization: Bearer <jwt aud=identity>`, `X-Request-ID` and `X-Client-IP` (Issues #4) |
| `lib/api/payroll.ts` | `payrollClient(ctx)`: the same over `PayrollPaths` (`aud=payroll`) |
| `lib/route.ts` | The `route()` wrapper: the request pipeline (§3.1) |
| `lib/rate-limit.ts` | Sliding-window limiter on Redis (§5.1) |
| `lib/client-ip.ts` | `clientIp(headers, hops)` from `X-Forwarded-For` and `TRUSTED_PROXY_HOPS` (§5.2) |
| `lib/origin.ts` | `checkOrigin(req)` for mutations (§5.4) |
| `lib/body.ts` | `readJson(req, schema, limit)`: a size-capped body read plus Zod parsing (§5.3) |
| `lib/errors.ts` | `BffError`, the error catalog and `mapUpstreamError()` (§3.3); `toErrorResponse()` |
| `lib/logger.ts` | The pino root logger: trace mixin, redactor (§7.1) |
| `lib/metrics.ts` | OTel meter `web`: instruments (§7.2) |
| `lib/csp.ts` | `buildCsp(nonce, env)` and `newNonce()` (§5.5) |
| `lib/request-context.ts` | `AsyncLocalStorage<{requestId, logger}>` so the clients and the logger see the request ID |
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
| `src/instrumentation.ts` | `register()`: on `nodejs` only, runs `loadConfig()` (exits in uat and prod on a failed check), then `registerOTel` (§7.4). `onRequestError` logs `web.server_error` |
| `src/proxy.ts` | Cookie-presence gate, CSP nonce, `x-request-id` (§5.5) |
| `src/components/ui/*` | shadcn/ui primitives (Button, Dialog, AlertDialog, Table, Tabs, Badge, Form, Input, Select, Checkbox, Toast, Skeleton) |
| `src/components/app-shell.tsx`, `nav.tsx` | Layout, skip link, nav items filtered by role, user menu with sign-out |
| `src/components/role-gate.tsx` | Shows children only for the given roles. This is a UI convenience; the server enforces |
| `src/components/providers.tsx` | QueryClientProvider (defaults in §6.1) and the Toaster |
| `src/components/web-vitals.tsx` | `useReportWebVitals` → `navigator.sendBeacon("/api/telemetry")`; a `window.onerror` / `unhandledrejection` reporter |
| `src/components/money.tsx`, `data-table.tsx`, `cursor-pager.tsx`, `error-banner.tsx`, `confirm-dialog.tsx`, `live-region.tsx`, `forbidden.tsx` | Shared UI |
| `src/components/forms/{login-form,accept-form,reset-request-form,reset-confirm-form,employee-form,settings-form,invite-dialog,user-edit-dialog,run-create-form}.tsx` | Forms using react-hook-form with the shared Zod schemas |
| `src/components/runs/{run-header,run-actions,run-status-badge,payslips-tab,hours-editor,payslip-dialog,payouts-tab,payout-status-badge,attempt-history,approve-button,retry-button}.tsx` | The run page (§6.3) |
| `src/components/audit/{auth-audit-table,payroll-audit-table}.tsx` | Audit tabs |

## 2. Data models

### 2.1 Redis keys (ACL user `web`, pattern `~web:*`)

| Key | Type | Fields / members | TTL | Requirement |
|---|---|---|---|---|
| `web:sess:<k>` | hash | `user_id` (uuid), `cid` (uuid), `roles` (JSON array of `admin`/`payroll_admin`/`approver`), `roles_version` (int), `created_at`, `last_seen`, `roles_checked_at` (epoch ms, int) | `min(SESSION_IDLE_TTL_S, created_at + SESSION_ABS_TTL_S − now)`, refreshed by the touch (§4.2) | PAY-106 |
| `web:user-sess:<user_id>` | set | the `<k>` of each live session of that user | `SESSION_ABS_TTL_S`, reset on every `SADD` | PAY-106, 107 |
| `web:rl:<scope>:<subject>:<window>` | string (counter) | `scope` ∈ `ip`, `sess`, `tel`; `subject` = 16 hex chars of `HMAC-SHA256(SESSION_SECRET, ip or k)`; `window` = `floor(now_s / 60)` | 120 s | PAY-603 |

- `<k>` = `HMAC-SHA256(SESSION_SECRET, <session id>)` as 64 hex chars. The cookie holds the raw ID (256 random bits, base64url, 43 chars). Redis holds only the HMAC, so a Redis dump or `MONITOR` output yields no usable cookie (Issues #16).
- **The JWT `sid` claim** is the first 32 hex chars of `<k>`. It's stable per session, can't be turned back into the cookie, and lets service logs be correlated with a session.
- **ACL** (provisioned with the other ACL users in the deploy bootstrap):

  ```
  user web on >${REDIS_WEB_PASSWORD} ~web:* -@all
    +get +set +del +incr +expire +pexpire +pttl +hset +hget +hgetall
    +sadd +srem +smembers +multi +exec +ping
  ```

  So web can't touch `stream:*`, `identity:*` or `payroll:*`, and can't `XADD` (PAY-607).

### 2.2 Session types (`lib/session.ts`)

```ts
export type Role = "admin" | "payroll_admin" | "approver";
export interface SessionRecord {            // the Redis hash, parsed
  user_id: string; cid: string; roles: Role[]; roles_version: number;
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
- Browsers treat `localhost` and `127.0.0.1` as secure contexts, so `Secure` works over plain http in dev and e2e.

### 2.3 Request schemas (`lib/schemas/`, shared with the forms)

These are common to all of them:
- `Email = z.string().trim().toLowerCase().email().max(254)`
- `Password = z.string().min(12).max(256)` (12 is PAY-103; the BFF checks it early so the user gets instant feedback, and identity stays authoritative)
- `Uuid = z.string().uuid()`
- `IsoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/)`
- `Cursor = z.string().max(512).optional()`
- `Limit = z.coerce.number().int().min(1).max(100).default(50)`

Every object schema is `.strict()`: an unknown key gives 422.

| Schema | Shape | Requirement |
|---|---|---|
| `LoginBody` | `{email: Email, password: z.string().min(1).max(256)}` (no minimum length at sign-in) | PAY-104 |
| `AcceptBody` | `{token: z.string().regex(/^[A-Za-z0-9_-]{20,128}$/), password: Password}` | PAY-103 |
| `ResetRequestBody` | `{email: Email}` | PAY-105 |
| `ResetConfirmBody` | `{token: <as AcceptBody>, password: Password}` | PAY-105 |
| `ListQuery` | `{cursor: Cursor, limit: Limit}` | PAY-108, 202, 303 |
| `InviteBody` | `{email: Email, name: z.string().trim().min(1).max(200), roles: z.array(RoleEnum).min(1).max(3)}`, with roles required to be unique | PAY-101 |
| `UserPatchBody` | `{roles?: RoleEnum[] (min 1, unique), status?: z.enum(["active","inactive"])}`; at least one field | PAY-108 |
| `SettingsBody` | `{name: 1–200, tax_rate_bps: int 0–5000, retirement_bps: int 0–2000, timezone: one of Intl.supportedValuesOf("timeZone"), biweekly_anchor: IsoDate that is a Monday, require_dual_approval: boolean}` | PAY-200 |
| `EmployeeCreateBody` | A discriminated union on `pay_type`. Common fields: `name` 1–200, `email: Email`, `schedule: "biweekly"\|"monthly"`, `bank_routing: /^\d{9}$/` with the ABA checksum, `bank_account: /^\d{4,17}$/`. Then `salaried` with `annual_salary_cents: int 1–1_000_000_000`, or `hourly` with `hourly_rate_cents: int 1–100_000` | PAY-201 |
| `EmployeePatchBody` | Every field of `EmployeeCreateBody` optional, plus `status?: "active"\|"inactive"`; at least one field | PAY-203 |
| `EmployeeListQuery` | `ListQuery & {status?: "active"\|"inactive"}` | PAY-202 |
| `PayRunCreateBody` | `{schedule: "biweekly"\|"monthly", period_start: IsoDate}` | PAY-300 |
| `HoursBody` | `{entries: z.array({employee_id: Uuid, hours_hundredths: int 0–74_400}).min(1).max(500)}`, with unique `employee_id`. The per-schedule bound (33,600 biweekly) is payroll's (PAY-301) | PAY-301 |
| `IdempotencyKeyHeader` | `Uuid`. The browser generates it with `crypto.randomUUID()` | PAY-400 |
| `TelemetryBody` | A discriminated union. `{type: "vital", name: "CLS"\|"FCP"\|"INP"\|"LCP"\|"TTFB", value: number ≥ 0, rating: "good"\|"needs-improvement"\|"poor", route: string ≤ 100}`, or `{type: "error", message: string ≤ 500, route: string ≤ 100}` | §7.5 |
| `IdParam` | `{id: Uuid}` (and `payslipId`, `payoutId`) | all `[id]` routes |

Forms enter money, rates and hours as text. `lib/money.ts` converts them with string-integer parsing:
- money: `^\d{1,8}(\.\d{1,2})?$` → cents;
- percent: `^\d{1,2}(\.\d{1,2})?$` → basis points;
- hours: `^\d{1,3}(\.\d{1,2})?$` → hundredths.

There's no float anywhere on the path (ADR 0006).

### 2.4 Response schemas

The generated types in `packages/api-client` are the contract. The Zod schemas below mirror them. A type-level test (`expectTypeOf<z.infer<typeof X>>().toEqualTypeOf<...>()`) fails when they drift. The BFF parses every service response it returns, because these responses are shown to users (`architecture.md` §4).
- **Default mode is strip:** unknown keys are dropped.
- **`Employee` and `Payout` are `.strict()`.** Any unexpected key (for example a `bank_account` field) fails parsing. The BFF then returns 500 `internal_error` and logs `bff.response_invalid`, so a full bank number can't leak through the BFF (PAY-202, PAY-501, PAY-NFR-08).

| Schema | Fields | Requirement |
|---|---|---|
| `Page<T>` | `{items: T[], next_cursor: string \| null}` (Issues #20) | PAY-108, 202, 303 |
| `User` | `{id, email, name, roles: Role[], status: "pending"\|"active"\|"inactive", roles_version: int, invite_id: uuid \| null}` (Issues #10) | PAY-101, 108 |
| `Me` | `User` without `invite_id` | PAY-107 |
| `Settings` | the `SettingsBody` fields | PAY-200 |
| `Employee` | `{id, name, email, pay_type, annual_salary_cents: int\|null, hourly_rate_cents: int\|null, schedule, account_last4: /^\d{4}$/, status, bank_updated_at: datetime\|null, deactivated_at: datetime\|null, created_at, updated_at}` | PAY-201–203 |
| `PayRun` | `{id, schedule, period_start, period_end, pay_date, period_index, status: "draft"\|"calculated"\|"processing"\|"paid"\|"partially_failed", totals: {gross_cents, tax_cents, retirement_cents, net_cents, funding_required_cents} \| null, payout_counts: {requested, paid, failed}, calculated_by: uuid\|null, approved_by: uuid\|null, approved_at: datetime\|null, processing_since: datetime\|null, created_at}` | PAY-303, 413 |
| `Payslip` | `{id, employee_id, employee_name, pay_type, schedule, annual_salary_cents\|null, hourly_rate_cents\|null, hours_hundredths: int\|null, gross_cents, tax_cents, retirement_cents, net_cents}` | PAY-303 |
| `Payout` | `{id, employee_name, status: "requested"\|"paid"\|"failed", no_transfer: boolean, current_attempt: int, attempts: [{attempt, status: "requested"\|"indeterminate"\|"needs_operator"\|"settled"\|"rejected", failure_reason: string\|null, bank_reference: string\|null, requested_at, final_at: datetime\|null}]}` (Issues #11) | PAY-501, 413 |
| `AuthAuditEvent` | `{id, at, actor_user_id\|null, event, target_user_id\|null, ip_hash\|null}` | PAY-109 |
| `PayrollAuditEvent` | `{id, at, actor_user_id, event, entity_type, entity_id, changes: Record<string, unknown> \| null}` (Issues #19) | plan §8 |
| `ErrorBody` | `{error: {code: string, message: string, request_id: string, details?: unknown}}` | PAY-602 |

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
  session: Session | null; requestId: string; log: pino.Logger; ip: string;
  identity: IdentityClient; payroll: PayrollClient;   // built for this request (user token, or service:web)
}
export function route<B, Q, P>(def: RouteDef<B, Q, P>):
  (req: NextRequest, ctx: { params: Promise<Record<string, string>> }) => Promise<Response>;
```

**Steps.** Each one may throw a `BffError`, which step 10 turns into a response.

1. **Context.** Read `x-request-id` from the request; `proxy.ts` set it. Otherwise generate a UUIDv7. Set `AsyncLocalStorage`. Read `traceId` from the active span.
2. **IP rate limit** (`scope=ip`, `BFF_RATE_LIMIT_IP` per minute). This applies to every route except `/api/livez`, `/api/readyz` and `/api/telemetry`, which has its own limit (§7.5).
   - If Redis fails and `publicLimiter === "fail_closed"`: 503 `service_unavailable`.
   - Otherwise the limit fails open: count `rate_limiter_errors_total{component="web"}` and log `ratelimit.backend_error`.
3. **Origin**, for POST, PUT, PATCH and DELETE (§5.4): 403 `bad_origin`.
4. **Session.**
   - `auth: {roles}` → `requireSession(roles)` (§4.3).
   - `"optional"` → load the session if the cookie is present; never throw 401.
   - `"public"` → skip.
5. **Session rate limit** (`scope=sess`, `BFF_RATE_LIMIT_SESSION` per minute), when there's a session. It fails open.
6. **Params and query:** Zod. A failure gives 422 `validation_error` with `details: [{field, issue}]`. A malformed `[id]` also gives 422.
7. **Body:** `readJson(req, def.body, def.bodyLimit)` (§5.3): 413 `payload_too_large`, 400 `invalid_json`, or 422 `validation_error`.
8. **Clients.** Build `identity` and `payroll` with a token provider bound to the session: `sub=user_id`, the session's roles, `cid`, `sid`. Public routes use `sub=service:web`.
9. **Handler.**
10. **Finish.**
    - Map any thrown error with `toErrorResponse()` (§3.3).
    - Set `x-request-id`, `x-trace-id` and `Cache-Control: no-store` on every response, including errors (PAY-604).
    - Record `bff_request_duration_seconds{route=def.name, status}`.
    - Log `http.request` (§7.1).

### 3.2 BFF route table

Conventions for the table:
- **"Origin ✓"** means step 3 applies.
- **JWT:** `U` = a user token (`sub=<user_id>`, the session's roles, `cid` and `sid`). `S` = a service token (`sub=service:web`, `roles=[]`, `cid=COMPANY_ID`; Issues #1).
- **Timeouts:** every call uses connect 1 s and read 3 s, except calculate and approve, which read for 10 s (§3.4.2).
- **Pass-through responses** are the parsed §2.4 schema. Lists are `Page<T>`.
- **Error mapping:** every route also maps these:
  - 401 `unauthenticated` (no or ended session);
  - 403 `forbidden_role` (BFF or service);
  - 403 `bad_origin`, 413 `payload_too_large`, 422 `validation_error`, 429 `rate_limited`;
  - 503 `service_unavailable` (upstream down, timeout, breaker open or Redis down), and 503 `db_timeout` / `db_unavailable` forwarded as 503 `service_unavailable`;
  - 500 `internal_error`.

  The table lists only the route-specific codes. Their messages are in §3.3.

#### Auth

| Method, path | Session / role | Origin | Request (Zod) | Service call(s) | Response | Route-specific errors (status code) | Req |
|---|---|---|---|---|---|---|---|
| POST `/api/auth/login` | public; IP limit **fails closed** | ✓ | `LoginBody` | identity `POST /v1/auth/login` (S) | 200 `{user: Me}` + `Set-Cookie`. An existing session cookie on the request is deleted first; the new session gets a fresh ID (§4.4) | 401 `invalid_credentials`; 429 `rate_limited` (identity's account/IP lockout, `Retry-After` forwarded) | PAY-104, 106 |
| POST `/api/auth/logout` | optional | ✓ | none | none | 204, cookie cleared (`Max-Age=0`). Deletes `web:sess:<k>` and `SREM web:user-sess:<user_id> <k>`. Idempotent without a session | — | PAY-106 |
| POST `/api/auth/accept` | public; **fails closed** | ✓ | `AcceptBody` | identity `POST /v1/invites/accept` (S) | 200 `{user: Me}` + `Set-Cookie` (new session) | 400 `invite_invalid`; 422 `password_too_weak` | PAY-103, 106 |
| POST `/api/auth/reset/request` | public; **fails closed** | ✓ | `ResetRequestBody` | identity `POST /v1/auth/password-reset/request` (S) | 202 `{}`, always (no account enumeration) | 429 `rate_limited` | PAY-105 |
| POST `/api/auth/reset/confirm` | public; **fails closed** | ✓ | `ResetConfirmBody` | identity `POST /v1/auth/password-reset/confirm` (S); then `revokeUserSessions(user_id, "password_reset")` when identity returns `user_id` (Issues #3) | 204; the current cookie is cleared | 400 `reset_invalid`; 422 `password_too_weak` | PAY-105 |
| GET `/api/auth/me` | any role | — | none | identity `GET /v1/users/me` (U). This is also a forced re-check: the §4.3 rules apply, and `roles_checked_at` is updated | 200 `Me` | — | PAY-106, 107 |

#### Users and invites (admin)

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/users` | admin | — | `ListQuery` | identity `GET /v1/users` (U) | 200 `Page<User>` | — | PAY-108 |
| POST `/api/users/invite` | admin | ✓ | `InviteBody` | identity `POST /v1/users/invite` (U) | 201 `User` | 409 `user_email_taken`; 422 (unknown or empty roles) | PAY-101 |
| PATCH `/api/users/[id]` | admin | ✓ | `IdParam`, `UserPatchBody` | identity `PATCH /v1/users/{id}` (U); on 200, `revokeUserSessions(id, "role_change" \| "deactivated")` | 200 `User` | 409 `cannot_modify_self`; 409 `last_admin`; 404 `user_not_found` (Issues #8) | PAY-107, 108 |
| POST `/api/invites/[id]/resend` | admin | ✓ | `IdParam` | identity `POST /v1/invites/{id}/resend` (U) | 200 `{}` | 409 `invite_not_pending`; 404 `invite_not_found` (Issues #8) | PAY-102 |

#### Settings

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/settings` | any | — | none | payroll `GET /v1/company/settings` (U) | 200 `Settings` | — | PAY-200 |
| PUT `/api/settings` | admin | ✓ | `SettingsBody` | payroll `PUT /v1/company/settings` (U) | 200 `Settings` | 422 (out of range) | PAY-200 |

#### Employees

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/employees` | any | — | `EmployeeListQuery` | payroll `GET /v1/employees` (U) | 200 `Page<Employee>` | — | PAY-202 |
| POST `/api/employees` | payroll_admin | ✓ | `EmployeeCreateBody` | payroll `POST /v1/employees` (U) | 201 `Employee` | 409 `employee_email_taken`; 422 (fields in `details`) | PAY-201 |
| GET `/api/employees/[id]` | any | — | `IdParam` | payroll `GET /v1/employees/{id}` (U) | 200 `Employee` | 404 `employee_not_found` | PAY-202 |
| PATCH `/api/employees/[id]` | payroll_admin | ✓ | `IdParam`, `EmployeePatchBody` | payroll `PATCH /v1/employees/{id}` (U) | 200 `Employee` | 404 `employee_not_found`; 409 `employee_email_taken`; 422 | PAY-203 |

#### Pay runs, payslips, payouts

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/pay-runs` | any | — | `ListQuery` | payroll `GET /v1/pay-runs` (U) | 200 `Page<PayRun>` | — | PAY-303 |
| POST `/api/pay-runs` | payroll_admin | ✓ | `PayRunCreateBody` | payroll `POST /v1/pay-runs` (U) | 201 `PayRun` | 409 `pay_run_exists`; 422 `invalid_period_start`; 409 `no_employees`; 409 `employee_in_overlapping_run` (`details.employee_ids` forwarded) | PAY-300 |
| GET `/api/pay-runs/[id]` | any | — | `IdParam` | payroll `GET /v1/pay-runs/{id}` (U) | 200 `PayRun`. Polled every 2 s while `processing` | 404 `pay_run_not_found` (Issues #8) | PAY-303, 413 |
| DELETE `/api/pay-runs/[id]` | payroll_admin | ✓ | `IdParam` | payroll `DELETE /v1/pay-runs/{id}` (U) | 204 | 409 `pay_run_locked`; 404 `pay_run_not_found` | PAY-304 |
| PUT `/api/pay-runs/[id]/hours` | payroll_admin | ✓ | `IdParam`, `HoursBody` | payroll `PUT /v1/pay-runs/{id}/hours` (U) | 200 `PayRun` (now `draft`) | 409 `pay_run_locked`; 422 `not_hourly`; 422 (bounds) | PAY-301 |
| POST `/api/pay-runs/[id]/calculate` | payroll_admin | ✓ | `IdParam` | payroll `POST /v1/pay-runs/{id}/calculate` (U; **read 10 s**) | 200 `PayRun` (`calculated`) | 422 `hours_missing` (`details.employee_ids` forwarded); 409 `pay_run_locked` | PAY-302 |
| POST `/api/pay-runs/[id]/approve` | approver | ✓ | `IdParam`; header `Idempotency-Key: IdempotencyKeyHeader` (required: missing or not a UUID gives 422 `validation_error`) | payroll `POST /v1/pay-runs/{id}/approve` (U; **read 10 s**; `Idempotency-Key` forwarded unchanged; never retried by the BFF) | 200 `PayRun` (`processing` or `paid`) | 409 `pay_run_not_approvable`; 403 `approver_must_differ` (approve message); 422 `idempotency_key_reused` | PAY-400, 401 |
| GET `/api/pay-runs/[id]/payslips` | any | — | `IdParam` | payroll `GET /v1/pay-runs/{id}/payslips` (U) | 200 `{items: Payslip[]}` | 404 `pay_run_not_found` | PAY-303 |
| GET `/api/pay-runs/[id]/payslips/[payslipId]` | any | — | `IdParam` + `payslipId` | payroll `GET /v1/pay-runs/{id}/payslips/{payslip_id}` (U) | 200 `Payslip` | 404 `payslip_not_found` (Issues #8) | PAY-303 |
| GET `/api/pay-runs/[id]/payouts` | any | — | `IdParam` | payroll `GET /v1/pay-runs/{id}/payouts` (U) | 200 `{items: Payout[]}` | 404 `pay_run_not_found` | PAY-501, 413 |
| POST `/api/pay-runs/[id]/payouts/[payoutId]/retry` | approver | ✓ | `IdParam` + `payoutId` | payroll `POST /v1/pay-runs/{id}/payouts/{payout_id}/retry` (U; never retried) | 200 `Payout` | 409 `payout_not_retryable`; 409 `retry_limit_reached`; 409 `employee_bank_details_missing`; 403 `approver_must_differ` (retry message); 404 `payout_not_found` | PAY-500 |

#### Audit and infrastructure

| Method, path | Role | Origin | Request | Service call | Response | Route-specific errors | Req |
|---|---|---|---|---|---|---|---|
| GET `/api/audit/auth` | admin | — | `ListQuery` | identity `GET /v1/audit-log` (U) | 200 `Page<AuthAuditEvent>` | — | PAY-109 |
| GET `/api/audit/payroll` | admin | — | `ListQuery` | payroll `GET /v1/audit-events` (U) | 200 `Page<PayrollAuditEvent>` | — | plan §8 |
| POST `/api/telemetry` | optional; own limit `BFF_TELEMETRY_RATE_LIMIT_IP`, failing open | ✓ | `TelemetryBody`, limit 8 KB | none | 204 | 429 `rate_limited` | §7.5 |
| GET `/api/livez` | public, no limit | — | none | none | 200 `{status:"ok"}` | — | PAY-605 (web) |
| GET `/api/readyz` | public, no limit | — | none | Redis `PING` | 200, or 503 `service_unavailable` | — | PAY-605 (web) |

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
- **`details`** is forwarded only for 422 `validation_error` (field names, with the problem but never the submitted value), `employee_in_overlapping_run` and `hours_missing` (`employee_ids`).
- **Internals are never forwarded:** no upstream message, stack trace, URL or SQL.

| Code | Status | User-facing message | Source |
|---|---|---|---|
| `unauthenticated` | 401 | "Your session has ended. Please sign in again." | BFF (PAY-106) |
| `forbidden_role` | 403 | "You don't have permission to do that." | BFF, services (PAY-601) |
| `bad_origin` | 403 | "This request was blocked for your security. Reload the page and try again." | BFF (PAY-604) |
| `payload_too_large` | 413 | "That request is too large." | BFF, services (PAY-604) |
| `invalid_json` | 400 | "The request couldn't be read." | BFF |
| `validation_error` | 422 | "Some fields need attention." (plus `details`) | BFF; also service 422s without a specific code (Issues #9) |
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
| `user_not_found`, `invite_not_found` | 404 | "That user wasn't found." / "That invite wasn't found." | identity (Issues #8) |
| `employee_email_taken` | 409 | "An employee with this email already exists." | payroll (PAY-201) |
| `employee_not_found` | 404 | "That employee wasn't found." | payroll (PAY-202) |
| `pay_run_exists` | 409 | "A pay run already exists for this schedule and period." | payroll (PAY-300) |
| `invalid_period_start` | 422 | Monthly: "Monthly runs must start on the 1st of a month." Biweekly: "Biweekly runs must start on a period start date (every 14 days from {biweekly_anchor})." | payroll (PAY-300) |
| `no_employees` | 409 | "No active employees are on this schedule." | payroll (PAY-300) |
| `employee_in_overlapping_run` | 409 | "{n} employee(s) are already in another run that covers these dates." (lists them) | payroll (PAY-300) |
| `pay_run_locked` | 409 | "This run is locked because it has been approved." | payroll (PAY-301, 302, 304) |
| `not_hourly` | 422 | "Hours can only be entered for hourly employees." | payroll (PAY-301) |
| `hours_missing` | 422 | "Enter hours for every hourly employee before calculating ({n} missing)." | payroll (PAY-302) |
| `pay_run_not_approvable` | 409 | "This run can't be approved: it has already been approved, or it isn't calculated." | payroll (PAY-400) |
| `idempotency_key_reused` | 422 | "This approval was already submitted with different details. Reload and try again." | payroll (PAY-400) |
| `approver_must_differ` (approve route) | 403 | **"Dual approval is on: you can't approve this run because you calculated it, or you changed bank details for an employee in it. Ask another approver."** | payroll (PAY-401, ADR 0005) |
| `approver_must_differ` (retry route) | 403 | **"Dual approval is on: you can't retry this payout because you last changed this employee's bank details. Ask another approver."** | payroll (PAY-500, ADR 0005) |
| `payout_not_retryable` | 409 | "Only failed payouts can be retried. Payouts awaiting the bank or blocked can't be retried." | payroll (PAY-500) |
| `retry_limit_reached` | 409 | "This payout has reached the limit of 5 attempts." | payroll (PAY-500) |
| `employee_bank_details_missing` | 409 | "This employee is inactive or has no bank details. Update the employee first." | payroll (PAY-500) |
| `pay_run_not_found`, `payslip_not_found`, `payout_not_found` | 404 | "That pay run / payslip / payout wasn't found." | payroll (Issues #8) |
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
  { sub: string; roles: Role[]; cid: string; sid?: string }): Promise<string> {
  return new SignJWT({ roles: p.roles, cid: p.cid, ...(p.sid ? { sid: p.sid } : {}) })
    .setProtectedHeader({ alg: "EdDSA", kid: config().INTERNAL_JWT_KID, typ: "JWT" })
    .setIssuer("web").setAudience(aud).setSubject(p.sub)   // user id, or "service:web" on public flows
    .setIssuedAt().setExpirationTime("60s").setJti(crypto.randomUUID())
    .sign(await signingKey());
}
```

- **Claims:** exactly `iss=web`, `aud` (`identity` or `payroll`), `sub`, `roles`, `cid`, `sid` (user tokens only), `iat`, `exp = iat + 60`, `jti` (PAY-600).
- **A fresh token per upstream call.** Ed25519 signing costs about 50 µs. There's no cache, so a token never outlives its request by much.
- **User tokens:** `sub=<user_id>`, roles and `cid` from the session, `sid` = `session.sid`.
- **Public flows** (login, accept, reset request and confirm): `sub=service:web`, `roles=[]`, `cid=COMPANY_ID` (Issues #1).
- **Never logged.** The `authorization` key is redacted (§7.1), and a test asserts that no captured log line contains the token.
- **Key rotation** (`auth.md`): add the new public key to every service's `INTERNAL_JWT_PUBLIC_KEYS` first, then change `INTERNAL_JWT_PRIVATE_KEY` and `INTERNAL_JWT_KID`, then remove the old key after 60 s.

#### 3.4.2 Timeouts, retries and breaker (`lib/api/resilient-fetch.ts`, `lib/api/breaker.ts`)

`openapi-fetch` clients are created per request with `createClient<IdentityPaths>({ baseUrl: IDENTITY_BASE_URL, fetch: resilientFetch("identity", opts) })`; payroll is the same. A middleware (`client.use`) adds `Authorization`, `X-Request-ID` and `X-Client-IP`.

| Policy | Value | Source |
|---|---|---|
| Connect timeout | `UPSTREAM_CONNECT_TIMEOUT_MS` = 1000. An `undici` `Agent({ connect: { timeout } })` is passed as `dispatcher`, with one agent per target (keep-alive) | plan §4 |
| Read timeout, per attempt | `UPSTREAM_READ_TIMEOUT_MS` = 3000. `UPSTREAM_SLOW_READ_TIMEOUT_MS` = 10000 for `calculate` and `approve`. Implemented as `AbortSignal.any([req.signal, AbortSignal.timeout(ms)])` | plan §4 |
| Retries | **GET only**, at most `UPSTREAM_GET_RETRIES` = 2 (so 3 attempts). Retried on HTTP **429, 502, 503 and 504**, and on connection failures where nothing was sent (`ECONNREFUSED`, `ECONNRESET` before the request was written, connect timeout). A read timeout isn't retried. POST, PUT, PATCH and DELETE are never retried | plan §4 |
| Backoff | Full jitter: `random(0, min(1000, 100 × 2^n))` ms. A `Retry-After` ≤ 1 s is honoured; a longer one isn't retried, and 429 `rate_limited` goes back with that header. No retry starts after `UPSTREAM_GET_DEADLINE_MS` (5000) from the first attempt | `architecture.md` §4 |
| Breaker | One per target (`identity`, `payroll`) per process: **closed** → **open** after `BREAKER_FAILURES` (5) consecutive failures. Failures are connect errors, timeouts, 502, 503 and 504; 4xx and 500 don't count. **open** → **half-open** after `BREAKER_RESET_S` (30). In half-open, one probe request runs and the rest fail fast; a success closes the breaker and a failure re-opens it. While it's open, calls fail immediately with 503 `service_unavailable`, with no network call | plan §4, §2.2 |
| Fallback | 503 `service_unavailable` with the mapped message. **Never a cached or stale response**: the BFF caches no service data | plan §4 |

Each attempt:
- increments `bff_upstream_requests_total{target, outcome}`;
- logs `bff.upstream_retry` (warning) when retried and `bff.upstream_error` (warning) when the final result is a failure;
- on a breaker transition, logs `external.breaker.opened` / `external.breaker.closed` and sets `circuit_breaker_state{target}`.

## 4. Sessions (`lib/session.ts`, `lib/require-session.ts`)

### 4.1 Creating a session (login and accept)

1. identity returns 200 with the user. The design needs `id`, `roles`, `roles_version`, `status` and `company_id` (Issues #2).
2. **Rotation.** If the request carried a session cookie, delete that session (`DEL web:sess:<old k>`, `SREM`). Then generate a new ID: `randomBytes(32).toString("base64url")`. A session ID is never reused across a sign-in (PAY-106; prevents fixation).
3. `k = HMAC(SESSION_SECRET, id)`, `now = Date.now()`. Run as one `MULTI`:
   - `HSET web:sess:<k> user_id … cid … roles … roles_version … created_at now last_seen now roles_checked_at now`;
   - `EXPIRE web:sess:<k> SESSION_IDLE_TTL_S`;
   - `SADD web:user-sess:<user_id> <k>`;
   - `EXPIRE web:user-sess:<user_id> SESSION_ABS_TTL_S`.
4. Set the cookie (§2.2). Log `auth.session.created {user_id_hash}` and increment `bff_sessions_created_total`.
5. If Redis fails at step 3: 503 `service_unavailable`, with no cookie set. This route was already fail-closed at its limiter.

### 4.2 TTLs and the touch

- **Idle TTL.** A request refreshes the idle TTL only when `now − last_seen ≥ SESSION_TOUCH_INTERVAL_S × 1000` (60 s), so a busy page doesn't write to Redis on every call. The touch is `HSET last_seen now` plus `PEXPIRE web:sess:<k> min(SESSION_IDLE_TTL_S×1000, created_at + SESSION_ABS_TTL_S×1000 − now)`. So idle expiry is accurate to within 60 s of 12 h.
- **Absolute TTL.** The TTL never runs past `created_at + SESSION_ABS_TTL_S`. `loadSession` also checks `now − created_at > SESSION_ABS_TTL_S×1000`; if so, it deletes the session and returns null, as a second guard.
- An expired session behaves like a missing one: 401 `unauthenticated` for `/api/*`, a redirect to `/login` for pages, and the cookie is cleared (PAY-106).
- **Stale index entries.** `web:user-sess:<user_id>` can hold keys that have already expired. `revokeUserSessions` tolerates that (a `DEL` of a missing key is a no-op), and session creation runs `SREM` for members whose `EXISTS` is 0, at most 20 at a time.

### 4.3 `requireSession(roles?)`

```ts
export async function requireSession(roles?: Role[]): Promise<Session>        // route handlers: throws BffError
export async function requireSessionForPage(roles?: Role[]): Promise<Session | "forbidden">
  // pages: redirect("/login?next=<path>") on 401; returns "forbidden" (the page renders <Forbidden/>) on 403;
  // throws on 503 (error.tsx shows "temporarily unavailable")
```

Algorithm (PAY-106, PAY-107, PAY-601, PAY-603):

1. Read the cookie `SESSION_COOKIE_NAME` with `await cookies()`. If it's missing → 401 `unauthenticated`.
2. `k = HMAC(SESSION_SECRET, cookie)`. Then `HGETALL web:sess:<k>`.
   - **Redis error or timeout** (`REDIS_SOCKET_TIMEOUT_MS` = 500) → 503 `service_unavailable`. Log `session.backend_error` (error) and increment `bff_session_backend_errors_total`. Never fall back to "no session", and never fall back to "allow".
   - **Empty** → 401 `unauthenticated`, and the response clears the cookie.
3. Parse with the `SessionRecord` Zod schema. If it's corrupt → delete it, then 401.
4. **Absolute check** (§4.2) → 401 if past.
5. **Role and status re-check**, when `now − roles_checked_at ≥ SESSION_ROLE_RECHECK_S × 1000` (60 s):
   - Call identity `GET /v1/users/me` with a user token (`sub=user_id`, the session's roles, `cid`, `sid`). This is single-flighted per `k` within the process, so parallel requests from one page share one call.
   - **200, and `status === "active"` and `roles_version === session.roles_version`** → `HSET roles_checked_at now`, also writing `roles` from the response.
   - **200 with a different `roles_version`, or a status other than `active`** → `revokeUserSessions(user_id, "roles_version_mismatch" | "inactive")`, log `auth.session.revoked`, then **401 `unauthenticated`**. The user signs in again and gets a new session with the new roles. If those roles no longer include the one an action needs, that action then returns 403 `forbidden_role`. This gives exactly PAY-107's "401 (session ended), or 403 when the roles shrank but the session was refreshed".
   - **401 or 404 from identity** (user gone, token rejected) → revoke this session, then 401.
   - **identity unavailable** (503, timeout or breaker open):
     - If `now − roles_checked_at < SESSION_RECHECK_GRACE_S × 1000` (300 s), **fail open**: keep the cached roles, don't update `roles_checked_at`, and log `auth.session.recheck_deferred` (warning). This keeps signed-in journeys working while identity is down (plan §12.1, "other journeys still work for signed-in users"). It's safe because no role change or reset can happen while identity is down.
     - **Past the grace** → 503 `service_unavailable`. A change made just before identity went down then takes effect at most `SESSION_RECHECK_GRACE_S` later (Issues #17).
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
- after a successful reset confirm, when identity returns `user_id` (Issues #3);
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
| `/api/readyz` | 503, so Kubernetes stops routing to the pod (`/livez` stays 200, so it isn't restarted) | — |
| logout | Best effort: the cookie is cleared, a Redis error is logged, and the response is 204 | `session.backend_error` |

The `ioredis` options (`enableOfflineQueue: false`, `commandTimeout` 500 ms) bound each failure to about 500 ms, so a Redis outage gives fast 503s rather than hung requests.

## 5. Security controls

### 5.1 Rate limiting (`lib/rate-limit.ts`, PAY-603)

```ts
export async function limit(scope: "ip" | "sess" | "tel", subject: string, perMinute: number):
  Promise<{ ok: true } | { ok: false; retryAfterS: number }>      // throws RateLimiterBackendError on Redis error
```

- **Algorithm: a sliding window built from two fixed windows.**
  1. `w = floor(now_s/60)`. In one pipeline: `INCR web:rl:<scope>:<subj>:<w>`, `EXPIRE … 120`, `GET web:rl:<scope>:<subj>:<w−1>`.
  2. `estimate = curr + prev × (1 − (now_s mod 60)/60)`.
  3. If `estimate > perMinute`, deny with `Retry-After = 60 − (now_s mod 60)` (rounded up, at least 1).

  It's shared across replicas through Redis. Plain `INCR` and `GET` mean no `EVAL` is needed (ACL §2.1).
- **Subjects:** `ip` → `HMAC16(clientIp)`; `sess` → `HMAC16(k)`; `tel` → `HMAC16(clientIp)`. No raw IP is stored in Redis.
- **Limits:**
  - `BFF_RATE_LIMIT_IP` (300/min) on every `/api/*` route except health and telemetry;
  - `BFF_RATE_LIMIT_SESSION` (120/min) after the session check;
  - `BFF_TELEMETRY_RATE_LIMIT_IP` (60/min) on telemetry only.

  The test environment (`compose.test.yaml`) raises the first two to 100000 so k6 at 30 req/s from one IP isn't throttled. A Vitest integration test proves that the production values limit.
- **Denied** → 429 `rate_limited` with `Retry-After`. Increment `rate_limited_total{scope}` and log `ratelimit.limited` (info: scope, route).
- **Redis error** → see §4.5. Each route sets `fail_closed` or `fail_open`.
- **The polling budget:** one run page polls two queries every 2 s while processing (§6.3), which is 60 requests a minute and fits inside 120. Two open run pages in one session reach the limit; the 429 is shown as "Live updates paused", and polling resumes after `Retry-After`.
- **Login abuse limits** (`AUTH_LOGIN_LIMIT_ACCOUNT` / `_IP`, lockout) are identity's (PAY-104). The BFF forwards identity's 429 and `Retry-After` unchanged. identity needs the client IP for the per-IP limit and `ip_hash` (Issues #4).

### 5.2 Client IP (`lib/client-ip.ts`)

```ts
export function clientIp(h: Headers, hops: number): string
```

- Split `X-Forwarded-For` on commas and trim each entry. With `n` entries, return entry `n − 1 − hops`; the right-most entry has index `n − 1`.
- If `n − 1 − hops < 0`, or the header is missing, return `"unknown"`, which shares one limiter bucket and is logged once per minute as `security.client_ip_unknown` (warning).
- The left-most value is never trusted on its own (`architecture.md` §9).
- **The dev value is 0 (no proxy).** This relies on the Next.js standalone server appending the socket peer address to `X-Forwarded-For` (Issues #5). `TRUSTED_PROXY_HOPS = 1` in uat and prod (one ingress).
- An entry that isn't a valid IPv4 or IPv6 address is treated as `"unknown"`.

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
- `Origin` must equal `new URL(PUBLIC_BASE_URL).origin` exactly: scheme, host and port.
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
2. `nonce = base64(crypto.getRandomValues(new Uint8Array(16)))`. `csp = buildCsp(nonce, ENV)`.
3. **Open paths** pass without a cookie:
   - pages: `/login`, `/accept`, `/reset`;
   - `/api/auth/login`, `/api/auth/logout`, `/api/auth/accept`, `/api/auth/reset/request`, `/api/auth/reset/confirm`;
   - `/api/telemetry`, `/api/livez`, `/api/readyz`.
4. **Cookie gate.** If the cookie `SESSION_COOKIE_NAME` is absent:
   - for `/api/*`, return 401 JSON `{error:{code:"unauthenticated", …, request_id}}`;
   - for pages, redirect 307 to `/login?next=<pathname+search>`, keeping the path only and dropping any fragment.

   Presence only: there's **no Redis lookup and no role check here** (PAY-601).
5. Otherwise `NextResponse.next({ request: { headers } })`, with the request headers `x-nonce`, `x-request-id` and `Content-Security-Policy: csp`. Next.js reads the CSP from the request to put the nonce on its own scripts.
6. **Response headers:** `Content-Security-Policy: csp` and `x-request-id`.

`buildCsp(nonce, env)` returns:

```
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
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | prod only (`ENV=prod`) |
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
  - dates are shown as calendar dates with no timezone conversion (period dates are `date` values in the company timezone, plan §2.1), and timestamps in the browser's locale.
- **Role gating.** `<RoleGate roles>` hides controls the user can't use, and the BFF still enforces. A page the user can't open at all renders `<Forbidden/>` (status 403 text, a link to the dashboard).

### 6.2 Pages

| Page | Purpose | Data (BFF) | Actions | Roles | Req |
|---|---|---|---|---|---|
| `/login` | Sign in | — | Submit `POST /api/auth/login`, then go to `next` (only a same-origin path that starts with `/` and not `//`), else `/dashboard`. 401 → "Email or password is incorrect."; 429 → countdown from `Retry-After`; 503 → "try again". A "Forgot password?" link goes to `/reset` | public | PAY-104 |
| `/accept` | Accept an invite | — | On mount, read `location.hash` (`#token=…`), keep it in component state, and call `history.replaceState(null, "", "/accept")`, so the token leaves the address bar and history. The token is never put in a query string, log or telemetry. Fields: password and confirm (≥ 12). Submit `POST /api/auth/accept`, then `/dashboard`. 400 `invite_invalid` → its message, with no form | public | PAY-103, NFR-07 |
| `/reset` | Request or confirm a reset | — | **No `#token`:** an email form → `POST /api/auth/reset/request` → always "If an account exists for that email, we've sent a reset link." **With `#token`:** read and strip it as on `/accept`; new password and confirm → `POST /api/auth/reset/confirm` → `/login` with the notice "Password changed. Sign in with your new password." | public | PAY-105 |
| `/dashboard` | The landing page after sign-in | `GET /api/auth/me`, `GET /api/pay-runs?limit=10`, `GET /api/employees?status=active&limit=1` (shows "50+" or the exact count when it has no `next_cursor`) | Cards: runs in `processing` (link), runs `partially_failed` (link, "needs attention"), the latest runs. Quick actions: "New pay run" and "Add employee" (payroll_admin) | any | PAY-303, 413 |
| `/employees` | The employee list | `GET /api/employees?status&cursor&limit=50` | Filter by status (active/inactive). Columns: name, email, pay type, pay basis (annual salary or hourly rate), schedule, bank `••••<account_last4>`, status. Cursor pager. "Add employee" (payroll_admin) | any; add: payroll_admin | PAY-202, J2 |
| `/employees/new` | Create an employee | `GET /api/settings` (for the schedule hint) | Form per `EmployeeCreateBody`: pay type switches between the salary and rate fields; inline ABA checksum; account 4–17 digits. Submit → `/employees/[id]`. 409 `employee_email_taken` shown on the email field | payroll_admin (others get `<Forbidden/>`) | PAY-201 |
| `/employees/[id]` | View and edit | `GET /api/employees/[id]` | View for every role. Edit (payroll_admin): the fields as on create. **Bank details are a separate "Change bank details" dialog** that takes a full new routing and account number and never shows the existing ones (only `••••last4`). Deactivate or reactivate with a confirm dialog. Shows `bank_updated_at` | view any; edit: payroll_admin | PAY-202, 203, ADR 0004 |
| `/settings` | Company settings | `GET /api/settings` | Read-only for non-admins. Admin form: name, tax rate (%), retirement rate (%), timezone (select), biweekly anchor (date; Mondays only), "Require dual approval" switch. Turning it off asks for confirmation: "Runs already calculated keep dual approval." Save → `PUT /api/settings` | view any; edit: admin | PAY-200, ADR 0005 |
| `/users` | Users and invites | `GET /api/users` | Table: name, email, roles, status (pending, active, inactive). "Invite user" dialog (email, name, role checkboxes; at least one) → `POST /api/users/invite`. Per row: "Edit roles", "Deactivate" or "Reactivate" (confirm) → `PATCH /api/users/[id]`, with these controls disabled on the admin's own row (`cannot_modify_self`). "Resend invite" for pending users → `POST /api/invites/[invite_id]/resend` | admin | PAY-101, 102, 108 |
| `/pay-runs` | The run list | `GET /api/pay-runs` | Columns: period, schedule, pay date, status badge, net total, payout counts. Newest period first; cursor pager. "New pay run" (payroll_admin) | any | PAY-303 |
| `/pay-runs/new` | Create a run | `GET /api/settings` | Schedule select. `period_start` date input with suggestions: monthly → the next first-of-month dates; biweekly → `biweekly_anchor + 14k` dates. The read-only derived `period_end` is shown for information; payroll derives the real value. Submit → `/pay-runs/[id]`. Errors: `pay_run_exists` (links to the existing run when it can be found in the list), `invalid_period_start`, `no_employees`, `employee_in_overlapping_run` (listing the employees) | payroll_admin | PAY-300 |
| `/pay-runs/[id]` | Run detail (§6.3) | `GET /api/pay-runs/[id]`, `/payslips`, `/payouts` | Hours, calculate, delete, approve, retry | view any; actions by role | PAY-301–304, 400, 401, 413, 500, 501 |
| `/audit` | Audit logs | `GET /api/audit/auth`, `GET /api/audit/payroll` | Two tabs; cursor paging; event, actor and target IDs, time. `ip_hash` is shown truncated. Changes on the payroll tab show field names only | admin | PAY-109, plan §8 |

### 6.3 The run page (`/pay-runs/[id]`)

**Header (`run-header.tsx`).**
- Period, schedule, pay date and status badge (`draft`, `calculated`, `processing`, `paid`, `partially_failed`), each with a text label and icon, not colour alone.
- Totals: gross, tax, retirement, net, and **funding required**.
- Payout counts (requested, paid, failed).
- `calculated_by` and `approved_by`, shown as "you" when the ID equals the session user, else a short ID (Issues #13). `approved_at`.

**Actions (`run-actions.tsx`).**

| Action | Shown when | Role | Call |
|---|---|---|---|
| Save hours | `draft` or `calculated`, and the run has hourly payslips | payroll_admin | `PUT /api/pay-runs/[id]/hours`. A calculated run goes back to draft; the UI says so before saving |
| Calculate | `draft` or `calculated` | payroll_admin | `POST …/calculate`. 422 `hours_missing` highlights the missing rows |
| Delete | `draft` or `calculated` | payroll_admin | Confirm dialog, then `DELETE`, then `/pay-runs` |
| Approve | `calculated` | approver | `approve-button.tsx` (§3.2). The confirm dialog shows the funding total and payout count. If `calculated_by === me`, a hint above the button says "You calculated this run. With dual approval on, another approver must approve it." The button stays enabled, because the snapshot decides (Issues #14). On 403 `approver_must_differ`, an inline `role="alert"` shows the dual-approval message (§3.3) |

**Tabs** (shadcn Tabs; `?tab=payslips|payouts`; the default is `payslips` before approval and `payouts` after):
- **Payslips** (`payslips-tab.tsx`, `hours-editor.tsx`). One row per payslip: employee, pay type, hours (an editable input for hourly rows in `draft`/`calculated` for payroll_admin; text otherwise), gross, tax, retirement, net. Clicking a row opens `payslip-dialog.tsx` (`GET …/payslips/[payslipId]`). The table footer shows the totals from the run summary, never sums computed in the browser.
- **Payouts** (`payouts-tab.tsx`, PAY-501). One row per payout: employee, badge, failure reason, attempt number, and a "History" disclosure (`attempt-history.tsx`) listing every attempt with its status, reason, bank reference, `requested_at` and `final_at`. A "Retry" button (`retry-button.tsx`, approver) appears on `failed` rows only. On 403 `approver_must_differ` it shows the retry message (§3.3). The other 409s show their messages.

**Payout badge (`lib/payout-badge.ts`).** The input is the payout plus its current attempt (`attempts` where `attempt === current_attempt`).

| Payout status | Current attempt | `no_transfer` | Badge | Notes |
|---|---|---|---|---|
| `paid` | any | true | "Paid (no transfer)" | net = 0 |
| `paid` | `settled` | false | "Paid" | bank reference in history |
| `failed` | `rejected` | — | "Failed" + reason | Retry shown (approver) |
| `requested` | `requested` | — | "Processing" | |
| `requested` | `indeterminate` | — | **"Awaiting bank"** | PAY-413 |
| `requested` | `needs_operator` | — | **"Blocked"** | PAY-413, PAY-409. Tooltip: "An operator must resolve this payout. It can't be retried." |

Failure reasons display as: `account_closed` → "Account closed", `insufficient_funds_at_bank` → "Insufficient funds at the bank". Anything else shows its code.

**Polling (PAY-413).**
- `useQuery(["pay-run", id], …, { refetchInterval: (q) => q.state.data?.status === "processing" ? 2000 : false, refetchIntervalInBackground: false })`.
- The payouts query uses the same interval, keyed off the run query's status, while the Payouts tab is open or a run-level banner needs it. Payout counts come from the run summary; attempt-level states (awaiting bank, blocked) come from the payouts list (Issues #12).
- **Polling stops** as soon as the status is `paid` or `partially_failed`. Both queries then refetch once, so the final state is complete.
- **Run-level banner while `processing`:**
  - "Paying employees: {paid} of {total} paid" (from counts);
  - plus "{n} awaiting bank", when any current attempt is `indeterminate`;
  - plus "{n} blocked: an operator must resolve these", when any is `needs_operator`.
- A retry moves the run back to `processing`, which restarts polling.

**Accessibility notes (PAY-NFR-14):**
- **Status changes** are announced through a polite `aria-live` region (`live-region.tsx`). It speaks only when the status or a count changes (for example "Run paid: 498 paid, 2 failed"), never on every poll.
- **Badges** carry text; colour is never the only signal. Contrast is at least 4.5:1 in the theme tokens.
- **Errors.** The dual-approval and other action errors render in `role="alert"`, linked to the button with `aria-describedby`. Form fields use `aria-invalid` and `aria-describedby` for their errors. After a failed submit, focus moves to the first invalid field.
- **Dialogs** (Radix) trap focus and return it to the trigger. Destructive confirms use `AlertDialog`.
- **Tables** use `<th scope="col">` and a `<caption>` (visually hidden where needed). The hours inputs have labels like "Hours for {employee}".
- **Tabs** follow the WAI-ARIA tabs pattern (Radix), with arrow keys.
- **Navigation:** every page has a unique `<title>` and one `<h1>`; a skip link goes to `<main>`.
- **Motion:** spinners respect `prefers-reduced-motion`. Polling never moves focus.

## 7. Observability

### 7.1 Logs (`lib/logger.ts`, pino)

- **Output:** JSON lines to stdout, and to OTLP through `pino-opentelemetry-transport` to Loki. Level `LOG_LEVEL`.
- **Base fields:** `service: "web"`, `env`, `version` (`APP_VERSION`).
- **Added per line:**
  - `ts` (ISO, `pino.stdTimeFunctions.isoTime`), `level` (label), `logger`, `event`, `msg`;
  - `trace_id` and `span_id` from `trace.getActiveSpan()` (a mixin);
  - `request_id` from the AsyncLocalStorage (the same mixin).

  These match the Python field names (`observability.md` §1).
- **Redaction** (PAY-NFR-07). A custom `formatters.log` walks the object to depth 6 and masks with `"[REDACTED]"` any key whose lower-cased name **contains** one of:
  - the shared deny-list: `password`, `token`, `authorization`, `cookie`, `account`, `routing`, `api_key`;
  - web additions: `set-cookie`, `secret`, `private_key`, `session`, `sid`, `idempotency`, `email`, `name`, `cents`, `salary`, `rate`.

  The custom formatter is used because pino's path-based `redact` can't match keys at any depth. The `email` and `name` additions keep personal data and amounts out of logs, which carry IDs and counts only (plan §11). `account` also masks `account_last4`, which is acceptable.
- **User-entered text** (client error messages) is logged as its length outside dev.
- **User IDs** in logs are `user_id_hash` = the first 16 hex chars of `HMAC(SESSION_SECRET, user_id)`. Raw user IDs appear only on spans (a bounded need for debugging).

| Event | Level | Fields | When | Req |
|---|---|---|---|---|
| `http.request` | info (debug for `/api/livez`, `/api/readyz`) | `method`, `route` (template), `status`, `duration_ms`, `user_id_hash?`, `code?` | Every BFF request, once | PAY-604, NFR-09 |
| `bff.upstream_error` | warning | `target`, `operation` (operationId), `status?`, `code?`, `error_kind` (timeout, connect, breaker_open, http), `attempts` | Final failure of an upstream call | PAY-602 |
| `bff.upstream_retry` | warning | `target`, `operation`, `attempt`, `status?`, `delay_ms` | Before each GET retry | plan §4 |
| `bff.upstream_auth_failed` | error | `target`, `operation` | Upstream 401 `invalid_token` | PAY-600 |
| `bff.unmapped_error` | warning | `target`, `status`, `code` | An upstream code not in the catalog | PAY-602 |
| `bff.response_invalid` | error | `target`, `operation`, `issues` (paths only) | A response fails its Zod schema | PAY-202, NFR-08 |
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
| `web.vitals` | info | `name`, `value`, `rating`, `route` | Telemetry | §7.5 |
| `web.client_error` | warning | `route`, `message` (dev) or `message_len` (uat, prod) | Telemetry | §7.5, NFR-07 |
| `web.server_error` | error | `route`, `digest`, `exc` | `onRequestError` | PAY-602 |
| `config.invalid` | error | `setting` (name only, never the value) | Startup check fails (the process then exits 1) | PAY-605 |

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

**Export.** The metrics leave through an OTLP HTTP metric exporter to the Collector. With the Collector's Prometheus exporter they appear with `service_name="web"`.
- The histogram instrument is created as name `bff_request_duration`, unit `s`, so the exporter's unit suffix produces `bff_request_duration_seconds` exactly. `lib/metrics.test.ts` asserts the exported name.
- Counters likewise drop `_total` from the instrument name, so the exporter's suffix produces the names above.

### 7.3 Request and trace IDs

- **`x-request-id`:**
  - generated in `proxy.ts` (UUIDv7, unless a well-formed one arrived);
  - passed to handlers as a request header;
  - sent to services as `X-Request-ID`;
  - returned on every BFF response and in `error.request_id`.
- **`x-trace-id`:** the active span's trace ID (32 hex), set in `route()` step 10 on every BFF response, error responses included (PAY-604). In prod, with 10% parent-based sampling, the ID is present even when the trace isn't stored. The e2e trace test runs in the test stack at sampler 1.0.

### 7.4 Tracing (`src/instrumentation.ts`)

```ts
import { registerOTel } from "@vercel/otel";
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { loadConfig } = await import("./lib/config");
  const cfg = loadConfig();                                   // uat/prod: logs config.invalid and exits 1
  const { IORedisInstrumentation } = await import("@opentelemetry/instrumentation-ioredis");
  const { PeriodicExportingMetricReader } = await import("@opentelemetry/sdk-metrics");
  const { OTLPMetricExporter } = await import("@opentelemetry/exporter-metrics-otlp-http");
  registerOTel({
    serviceName: "web",
    attributes: { "service.version": cfg.APP_VERSION, "deployment.environment.name": cfg.ENV },
    instrumentations: ["fetch", new IORedisInstrumentation({ dbStatementSerializer: (cmd) => cmd })], // command name only, never args
    instrumentationConfig: { fetch: { propagateContextUrls: [originRegex(cfg.IDENTITY_BASE_URL), originRegex(cfg.PAYROLL_BASE_URL)] } },
    metricReader: new PeriodicExportingMetricReader({ exporter: new OTLPMetricExporter(), exportIntervalMillis: 15000 }),
  });
}
export { onRequestError } from "./lib/on-request-error";
```

- **`propagateContextUrls`** is built from `IDENTITY_BASE_URL` and `PAYROLL_BASE_URL` themselves (`originRegex` escapes the origin and anchors it as `^<origin>/`). So the test override that routes them through `toxiproxy:<port>` still propagates `traceparent`. A missing host would silently break the J4 trace (PAY-NFR-09); `trace.spec.ts` catches that.
- **Endpoint:** `OTEL_EXPORTER_OTLP_ENDPOINT` → the Collector on port 4318 (HTTP/protobuf). Sampler: `OTEL_TRACES_SAMPLER=parentbased_traceidratio`, `OTEL_TRACES_SAMPLER_ARG`.
- **If the pinned `@vercel/otel` has no `metricReader` option,** `lib/metrics.ts` creates its own `MeterProvider` with the same reader and sets it globally.
- **Manual spans:** `bff.session.require` (attribute `session.recheck: boolean`) and `bff.session.recheck`.
- **Span hygiene:**
  - span attributes carry route templates, never raw query strings;
  - `url.full` on fetch spans to services contains only IDs (no tokens, which are never in URLs: PAY-103);
  - the ioredis serializer drops arguments, so no session key or IP hash appears in spans;
  - the Collector's redaction processor masks `token`-named attributes as defense in depth (plan §5 J1).

### 7.5 `/api/telemetry`

- **Browser side** (`web-vitals.tsx`): `useReportWebVitals((m) => send({type: "vital", name: m.name, value: m.value, rating: m.rating, route}))`. Errors from `window.onerror` and `unhandledrejection` → `send({type: "error", message: String(e.message).slice(0, 500), route})`.
- **Transport:** `send` uses `navigator.sendBeacon("/api/telemetry", new Blob([json], {type: "application/json"}))`, falling back to `fetch(…, {keepalive: true})`.
- **`route`** is the Next route template (from `useSelectedLayoutSegments` mapped to `/pay-runs/[id]`), never the real path with IDs, and never the fragment.
- **Server side:** auth `optional`; Origin check; 8 KB body; `BFF_TELEMETRY_RATE_LIMIT_IP` (60/min per IP), failing open. It logs `web.vitals` or `web.client_error` and returns 204. No metrics are derived from it in 0.1.0.

## 8. Configuration

All keys are read once by `lib/config.ts` (Zod).
- **Startup checks** run in uat and prod and refuse to start, logging `config.invalid` with the setting's name only (PAY-605, ADR 0008), when:
  - a required secret is missing or still has its dev default;
  - `INTERNAL_JWT_KID` starts with `dev-` or `test-`;
  - `PUBLIC_BASE_URL` isn't `https`;
  - `SESSION_COOKIE_NAME` isn't `__Host-sid`;
  - `SESSION_SECRET` is shorter than 32 bytes;
  - `INTERNAL_JWT_PRIVATE_KEY` doesn't import as Ed25519 PKCS8.
- **Secrets** come from `.env` (`scripts/dev-env.sh`) in dev and CI, and from External Secrets in uat and prod.

| Key | Type | Default dev / uat / prod | Notes | Req |
|---|---|---|---|---|
| `ENV` | `dev`\|`uat`\|`prod` | dev / uat / prod | Drives the startup checks, HSTS and CSP dev relaxations | PAY-605 |
| `APP_VERSION` | string | git SHA (build arg) | `service.version`, log `version` | NFR-09 |
| `PORT` | int | 4100 | Container port (HLD §1); host 4100 in Compose | NFR-13 |
| `PUBLIC_BASE_URL` | URL | `http://localhost:4100` / per env (https) / per env (https) | Origin check, open-redirect guard, CSP `upgrade-insecure-requests` | PAY-604 |
| `IDENTITY_BASE_URL` | URL | `http://identity:8000` (test override: via Toxiproxy) | Also feeds `propagateContextUrls` | NFR-09 |
| `PAYROLL_BASE_URL` | URL | `http://payroll:8000` (test override: via Toxiproxy) | Same | NFR-09 |
| `COMPANY_ID` | uuid | fixed dev UUID / per env | `cid` on `service:web` tokens (Issues #1) | PAY-600 |
| `INTERNAL_JWT_PRIVATE_KEY` | secret (PKCS8 PEM, Ed25519) | `.env` / External Secrets | Signs BFF tokens | PAY-600 |
| `INTERNAL_JWT_KID` | string | `dev-web-1` / non-`dev-` / non-`dev-` | JWT header `kid` | PAY-600 |
| `REDIS_URL` | URL | `redis://web@redis:6379/0` | Username `web` (ACL user); no password in the URL | PAY-607 |
| `REDIS_WEB_PASSWORD` | secret | `.env` / External Secrets | Passed to ioredis as `password` | PAY-607 |
| `REDIS_SOCKET_TIMEOUT_MS` | int | 500 | ioredis `commandTimeout` and `connectTimeout` | PAY-603 |
| `SESSION_SECRET` | secret (≥ 32 bytes) | `.env` / External Secrets | HMAC for session keys, limiter subjects, `user_id_hash` | PAY-106 |
| `SESSION_COOKIE_NAME` | string | `sid` / `__Host-sid` / `__Host-sid` | | PAY-106 |
| `SESSION_IDLE_TTL_S` | int | 43200 | | PAY-106 |
| `SESSION_ABS_TTL_S` | int | 604800 | | PAY-106 |
| `SESSION_ROLE_RECHECK_S` | int | 60 | | PAY-107 |
| `SESSION_RECHECK_GRACE_S` | int | 300 | New: fail-open window when identity is down (§4.3; Issues #17) | PAY-107 |
| `SESSION_TOUCH_INTERVAL_S` | int | 60 | New: minimum gap between idle-TTL refreshes | PAY-106 |
| `BFF_RATE_LIMIT_SESSION` | int per min | 120 (test env 100000) | | PAY-603 |
| `BFF_RATE_LIMIT_IP` | int per min | 300 (test env 100000) | | PAY-603 |
| `BFF_TELEMETRY_RATE_LIMIT_IP` | int per min | 60 | New | §7.5 |
| `BFF_BODY_LIMIT_BYTES` | int | 65536 | | PAY-604 |
| `TRUSTED_PROXY_HOPS` | int ≥ 0 | 0 / 1 / 1 | §5.2 | PAY-603 |
| `UPSTREAM_CONNECT_TIMEOUT_MS` | int | 1000 | New key for the plan §4 value | plan §4 |
| `UPSTREAM_READ_TIMEOUT_MS` | int | 3000 | Same | plan §4 |
| `UPSTREAM_SLOW_READ_TIMEOUT_MS` | int | 10000 | Calculate and approve | plan §4, NFR-05 |
| `UPSTREAM_GET_RETRIES` | int | 2 | | plan §4 |
| `UPSTREAM_GET_DEADLINE_MS` | int | 5000 | No retry is started after this | plan §4 |
| `BREAKER_FAILURES` / `BREAKER_RESET_S` | int | 5 / 30 | Shared names with the services (plan §2.2) | plan §2.2 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL | `http://otel-collector:4318` | HTTP/protobuf | NFR-09 |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | string / float | `parentbased_traceidratio` / 1.0 (dev, uat), 0.1 (prod) | plan §2.2 | NFR-09 |
| `LOG_LEVEL` | string | debug / info / info | | NFR-07 |
| `NEXT_TELEMETRY_DISABLED` | `1` | 1 | No Next.js telemetry egress | — |

Test-only (Playwright and k6, from `.env`; never read by the app): `BASE_URL`, `SEED_OWNER_EMAIL`, `SEED_OWNER_PASSWORD`, `SEED_APPROVER_PASSWORD`, `LOAD_USER_EMAIL`, `LOAD_USER_PASSWORD`, `SESSION_COOKIE`, `MAILPIT_URL` (`http://127.0.0.1:4102`), `TEMPO_URL` (`:4130`), `LOKI_URL` (`:4131`), `TOXIPROXY_URL` (`:4134`), `IDENTITY_DIRECT_URL` (`:4110`), `INTERNAL_JWT_PRIVATE_KEY`, `INTERNAL_JWT_KID` (for `e2e/helpers/mint-token.ts`).

## 9. Tests

The web gates are ESLint, Prettier, `tsc --noEmit` and Vitest coverage ≥ 80% lines (PAY-NFR-12). Every test cites its requirement ID in a comment.

### 9.1 Vitest (`apps/web/tests/`)

- **Unit tests** have no network: MSW (`setupServer`) mocks identity and payroll at `IDENTITY_BASE_URL` and `PAYROLL_BASE_URL`.
- **Redis:** a small fake implementing the `RedisLike` port (`get/set/hset/hgetall/sadd/smembers/del/incr/expire/pexpire/multi`), with a fault switch.
- **Clock:** fake timers (`vi.useFakeTimers()`).
- **Route handlers** are tested as functions: import `{ POST }` from the route file and call it with a `NextRequest` built by `tests/helpers/request.ts` (cookie, Origin, body, params Promise). `next/headers` `cookies()` is mocked from the request.
- **Integration files** (`*.int.test.ts`) use Testcontainers `redis:7-alpine` and run in the same CI job (`vitest --project integration`).

| Test file | Covers | Req |
|---|---|---|
| `tests/lib/session.test.ts` | Create (fields, `HMAC` key, index `SADD`, TTL = idle); rotation deletes the old session at sign-in; touch only after 60 s; `PEXPIRE` = `min(idle, abs remaining)`; absolute expiry → null and deleted; sign-out `DEL` + `SREM`; `revokeUserSessions` deletes all and the index; a corrupt record → deleted | PAY-106 |
| `tests/lib/require-session.test.ts` | No cookie → 401; unknown → 401 and cookie cleared; Redis error → 503 (never allow); re-check skipped < 60 s and called at ≥ 60 s (fake timers); `roles_version` mismatch → all sessions revoked + 401; status inactive → 401; same version → `roles_checked_at` updated; identity down within grace → allowed + `recheck_deferred`; past grace → 503; single-flight (5 parallel calls → 1 `/me`); roles mismatch → 403 with the session kept | PAY-106, 107, 601, 603 |
| `tests/lib/internal-jwt.test.ts` | Header `alg=EdDSA`, `kid`; claims `iss=web`, `aud` per target, `sub`, `roles`, `cid`, `sid` (user only), `exp − iat = 60`, unique `jti`; verifies with the public key via `jose.jwtVerify`; public flow `sub=service:web`, `roles=[]`, `cid=COMPANY_ID`; a missing key throws only on first use (build-safe) | PAY-600 |
| `tests/lib/api/resilient-fetch.test.ts` | Connect and read timeouts → 503; 10 s for calculate and approve; GET retried twice on 429/502/503/504 and connect errors, not on 500/4xx/read timeout; POST, PUT, PATCH and DELETE never retried; `Retry-After` ≤ 1 s honoured, > 1 s not retried; deadline 5 s; `X-Request-ID` and `Authorization` sent; metrics outcome labels | plan §4 |
| `tests/lib/api/breaker.test.ts` | Opens after 5 consecutive failures; fails fast with no fetch while open; half-open after 30 s lets one probe through; success closes, failure re-opens; 4xx and 500 don't count; `circuit_breaker_state` values | plan §4 |
| `tests/lib/errors.test.ts` | One case per §3.3 row: status, code and message; context-specific `approver_must_differ` messages; unknown 4xx → `request_failed`; unknown 5xx → 500; `invalid_token` → 500 + `bff.upstream_auth_failed`; `details` forwarded only for the listed codes; no upstream message leaks | PAY-602 |
| `tests/lib/rate-limit.test.ts` | Sliding-window estimate across a window boundary; deny at limit + 1 with `Retry-After`; subjects are hashed (no raw IP in keys); fail-closed vs fail-open per route mode; `rate_limiter_errors_total` | PAY-603 |
| `tests/lib/rate-limit.int.test.ts` | Real Redis: **two limiter instances share counts**; **production values** 120/min per session and 300/min per IP limit (the 121st and 301st requests are 429) | PAY-603 |
| `tests/lib/session.int.test.ts` | Real Redis: TTLs as set (`PTTL`), absolute cap, revocation across "two replicas"; the `web` ACL user can't `XADD stream:payout.requested` or `GET identity:*` (NOPERM) | PAY-106, 607 |
| `tests/lib/client-ip.test.ts` | hops 0/1/2 with 1–4 XFF entries; too few entries → `unknown`; invalid IP → `unknown`; never the left-most value when hops < n−1 | PAY-603 |
| `tests/lib/origin.test.ts` | Same origin passes; other scheme, host or port, `null`, or missing → 403 `bad_origin`; GET not checked | PAY-604 |
| `tests/lib/body.test.ts` | Content-Length > 64 KB → 413 without reading; a chunked body over the limit → 413; bad JSON → 400; wrong content type → 422; schema errors → 422 with field paths and no values | PAY-604 |
| `tests/lib/csp.test.ts`, `tests/proxy.test.ts` | CSP string per env (nonce, `strict-dynamic`, `unsafe-eval` dev only); unique nonce per request; `x-nonce` and CSP on the request headers; open paths pass without a cookie; `/api/*` without a cookie → 401 JSON; page → 307 `/login?next=`; with a cookie → passes **with no Redis access** (the redis module is not imported by `proxy.ts`) | PAY-604, 106, 601 |
| `tests/lib/logger.test.ts` | Inside a span: JSON line with `trace_id`, `span_id`, `request_id`, `event`; deep redaction of every deny-list key (nested, mixed case); a token, cookie and JWT set during a full route call never appear in captured output | PAY-NFR-07, NFR-09 |
| `tests/lib/metrics.test.ts` | `InMemoryMetricReader`: `bff_request_duration_seconds{route,status}` recorded once per request with the template route; failure paths increment `rate_limited_total{scope}`, `rate_limiter_errors_total{component="web"}`, `bff_upstream_requests_total{outcome}`; the exported Prometheus name is exact | PAY-NFR-04, 05, 10 |
| `tests/lib/config.test.ts` | Startup checks: each refused setting in uat/prod logs its name only (not the value) and exits; dev defaults accepted in dev | PAY-605 |
| `tests/lib/money.test.ts` | String → cents, percent → bps, hours → hundredths: boundaries, no float (property test with `fast-check` against BigInt arithmetic); `formatCents` | ADR 0006 |
| `tests/lib/payout-badge.test.ts` | Every row of the §6.3 badge table | PAY-413, 501 |
| `tests/schemas/*.test.ts` | Valid and invalid fixtures per request schema (§2.3) and response schema (§2.4); `Employee` and `Payout` `.strict()` reject `bank_account`/`routing`; `expectTypeOf` against `@payrun/api-client` types | PAY-201, 202, 501, 602 |
| `tests/api/auth.routes.test.ts` | login: Origin, fail-closed limiter on Redis error, `service:web` token with `aud=identity`, session created and rotated, cookie attributes per env, 401/429 mapping with `Retry-After`; logout; accept (`invite_invalid`, `password_too_weak`); reset request always 202; reset confirm revokes sessions; `/me` re-check | PAY-103–107 |
| `tests/api/users.routes.test.ts` | Admin only (403 for others); invite 201/409/422; PATCH revokes the target's sessions on roles or status; `cannot_modify_self`, `last_admin`; resend `invite_not_pending` | PAY-101, 102, 107, 108 |
| `tests/api/settings.routes.test.ts`, `employees.routes.test.ts` | Role matrix (GET any, writes admin/payroll_admin); `aud=payroll`; validation; `employee_email_taken`, `employee_not_found`; a response containing `bank_account` → 500 `internal_error` + `bff.response_invalid` | PAY-200–203, 601 |
| `tests/api/pay-runs.routes.test.ts` | Create/hours/calculate/delete role and error mapping (`pay_run_exists`, `invalid_period_start`, `no_employees`, `employee_in_overlapping_run` details, `pay_run_locked`, `not_hourly`, `hours_missing`); calculate uses the 10 s timeout; approve: approver only, missing or invalid `Idempotency-Key` → 422, key forwarded unchanged, never retried, `pay_run_not_approvable`, `approver_must_differ` approve message, `idempotency_key_reused` | PAY-300–304, 400, 401 |
| `tests/api/payouts.routes.test.ts` | Payslips and payouts pass-through; retry: approver only, never retried, `payout_not_retryable`, `retry_limit_reached`, `employee_bank_details_missing`, `approver_must_differ` retry message | PAY-500, 501 |
| `tests/api/audit.routes.test.ts`, `telemetry.route.test.ts`, `health.routes.test.ts` | Admin-only audit; telemetry 204, 8 KB limit, own rate limit, logs `web.vitals`/`web.client_error` (length only outside dev); `/api/readyz` 503 when Redis is down | PAY-109, NFR-07, PAY-605 |
| `tests/api/headers.test.ts` | Every BFF response (success and error, 401 included) has `x-request-id`, `x-trace-id` and `Cache-Control: no-store` | PAY-604 |
| `tests/components/run-page.test.tsx` | MSW on `/api/*`: polls every 2 s while `processing` (fake timers), stops at `paid`/`partially_failed`, restarts after a retry; "awaiting bank" and "blocked" badges and the banner counts; the live region announces only on change; actions disabled when a refetch fails | PAY-413 |
| `tests/components/approve-button.test.tsx` | A new UUID per click, the same key within one click's mutation; disabled while pending; the dual-approval message in `role="alert"`; network error → refetch run → success state if `processing` | PAY-400, 401 |
| `tests/components/retry-button.test.tsx`, `payouts-tab.test.tsx` | Retry only on failed rows for approvers; retry messages; attempt history rows | PAY-500, 501 |
| `tests/components/accept-form.test.tsx`, `reset-forms.test.tsx`, `login-form.test.tsx` | Token read from the fragment and stripped with `replaceState`; never sent in a URL; 12-character rule; mapped errors; `next` open-redirect guard | PAY-103–105 |
| `tests/components/employee-form.test.tsx`, `hours-editor.test.tsx`, `settings-form.test.tsx`, `users-page.test.tsx`, `role-gate.test.tsx` | Field validation and inline 422 details; ABA checksum; bank fields never prefilled; role-gated controls hidden | PAY-200–203, 301, 108, 601 |

### 9.2 Playwright (`apps/web/e2e/`)

- **Stack:** `scripts/stack.sh up` (profiles `core,obs,test` with `compose.test.yaml`), then `scripts/seed.py` (owner, `approver@payrun.local`, `LOAD_USER_EMAIL`, 10 employees including one account ending `0000`).
- **Projects:** one Chromium project; `baseURL = BASE_URL` (`http://127.0.0.1:4100`).
- **Helpers:**
  - `e2e/fixtures.ts`:
    - `asOwner` and `asApprover` page fixtures (separate browser contexts, signed in through the UI once and stored with `storageState`);
    - a console listener that **fails the test on any CSP violation** (`securitypolicyviolation` event or a console message containing "Content Security Policy"; ADR 0009);
    - `apiFetch(page, path, init)` runs `fetch` inside the page, so cookies and Origin behave as in the browser (`testing.md` §6).
  - `e2e/helpers/mailpit.ts`: `latestToken(email, kind: "accept" | "reset")`:
    1. Poll `GET {MAILPIT_URL}/api/v1/search?query=to:"<email>"` every 250 ms for up to 15 s, newest first.
    2. `GET /api/v1/message/{ID}` and match the `Text` body against `/\/(accept|reset)#token=([A-Za-z0-9_-]+)/`.
    3. Return the token and the full link. Tests open the link exactly as a user would (`page.goto(link)`).

    Each test uses a unique address (`e2e+<uuid>@payrun.local`), so there's no Mailpit cleanup and no cross-test interference.
  - `e2e/helpers/mint-token.ts`: signs a short-lived admin token with the dev key from `.env`, to call identity directly on `IDENTITY_DIRECT_URL` (`role-change.spec.ts` only).
  - `e2e/helpers/toxiproxy.ts`: enable or disable a proxy; resets all toxics in `afterEach`.

| Spec | Scenarios | Req |
|---|---|---|
| `access.spec.ts` | **J1:** the owner invites `e2e+…` with `[payroll_admin]`; the token comes from Mailpit; accept via the link; the URL has no `token` after load (fragment stripped) and no query token ever (request log listener); lands on the dashboard; `/api/auth/me` roles equal exactly `[payroll_admin]`; the nav hides admin items. Sign out → `/login`; the back button doesn't show data. Wrong password → "Email or password is incorrect." Expired or used invite link → `invite_invalid` message. Reset: request → Mailpit `reset` token → new password → old session gets 401 → sign in with the new password. Unauthenticated `/employees` → `/login?next=/employees`; `/api/employees` → 401 `unauthenticated`. The cookie has HttpOnly, SameSite=Lax and Secure | PAY-101, 103, 104, 105, 106 |
| `employees.spec.ts` | **J2:** admin sets the tax and retirement rates; payroll_admin adds a salaried and an hourly employee; the list shows `••••` + last 4 only, and the page HTML and `/api` responses never contain the full account number; edit bank details (dialog empty, not prefilled); deactivate and reactivate; duplicate email → message on the field; an approver-only user sees no "Add employee" and gets `<Forbidden/>` on `/employees/new` | PAY-200–203, 601, NFR-08 |
| `pay-run.spec.ts` | **J3:** create a monthly run; the hourly row requires hours (`hours_missing` on calculate); enter hours → calculate → totals shown; for every payslip row gross = net + tax + retirement, and funding = Σ net (read from the table); a duplicate period → `pay_run_exists` message; a biweekly run with a wrong start → `invalid_period_start`; delete a calculated run → the period is free again | PAY-300–304 |
| `approve-pay.spec.ts` | **J4:** dual approval on; the owner calculates; the owner's Approve → the dual-approval message (exact text, §3.3); the approver approves → status `processing` → polling (at most 2 s between `GET /api/pay-runs/{id}` requests, observed with `page.on("request")`) → `partially_failed` (the `0000` account) within 60 s; the failed row shows "Failed: Account closed"; a second approve via `apiFetch` with a new key → 409 `pay_run_not_approvable`; two parallel approves on a fresh run → exactly one 200 | PAY-400, 401, 413, 501 |
| `recover.spec.ts` | **J5:** continues from a `partially_failed` run; the owner (payroll_admin) edits the failed employee's bank details; the owner (also approver) clicks Retry → the retry `approver_must_differ` message; the approver retries → the run returns to `processing`, then `paid`; the history shows attempt 1 rejected and attempt 2 settled; retrying a paid payout via API → 409 `payout_not_retryable` | PAY-500, 501, 401 |
| `role-change.spec.ts` | (a) The admin removes `approver` from user B in `/users`; B's next approve → 401 at once (sessions revoked through `web:user-sess`) → `/login`; B signs in again → the approve button is hidden, and `apiFetch` approve → 403 `forbidden_role`. (b) **Re-check backstop:** demote user C by calling identity `PATCH /v1/users/{id}` directly (`mint-token.ts`; bypasses the BFF's revocation); poll C's mutations: allowed until the re-check, then 401 within `SESSION_ROLE_RECHECK_S` + 5 s (deadline 70 s) | PAY-107, 108 |
| `errors.spec.ts` | `/no-such-page` → 404 page; a forbidden page; **identity down** (Toxiproxy disables identity) → login shows "temporarily unavailable, try again" and the response is 503 `service_unavailable`, while a signed-in user can still list employees (payroll path); **payroll down** → employees page shows the error banner with a reference; `apiFetch` with an evil `Origin` (via `page.request` with explicit headers) → 403 `bad_origin`; a 70 KB body → 413 `payload_too_large`; response headers on a page and an `/api` route: CSP with a nonce and `strict-dynamic`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, `x-request-id`, `x-trace-id` (no HSTS in dev) | PAY-602, 604, plan §12.1 |
| `trace.spec.ts` | (a) **J4 trace:** the approver approves via `apiFetch` and reads `x-trace-id`; poll `GET {TEMPO_URL}/api/traces/<id>` for up to 30 s until spans exist from `service.name` = `web`, `payroll`, `payout-worker` and `bank-stub`; then Loki `query_range` `{service_name=~".+"} \| trace_id="<id>"` has at least one line each for web, payroll and payout-worker (bank-stub too if it logs inside the trace). (b) **No tokens in telemetry:** after an accept in this spec, Loki `{service_name=~".+"} \|= "<invite token>"` over the last 15 min returns 0 lines, and Tempo TraceQL `{ span.url.full =~ ".*<token>.*" \|\| span.url.query =~ ".*<token>.*" }` returns no traces | PAY-NFR-09, NFR-07 |
| `a11y.spec.ts` | `@axe-core/playwright` with the `wcag2a`/`wcag2aa` tags on `/login`, `/accept` (invalid-token state), `/reset`, `/dashboard`, `/employees`, `/employees/new`, `/settings`, `/users`, `/pay-runs`, `/pay-runs/[id]` (payslips tab), the payouts tab, `/audit`: no serious or critical violations. Keyboard-only path: tab to Approve, open the confirm dialog, Escape returns focus | PAY-NFR-14, NFR-12 |

Nightly runs the suite three times in a row (plan §12). No `waitForTimeout`: every wait polls with a deadline (`expect.poll`, `toPass`).

### 9.3 k6 (`load/`)

- **Shared helper** `load/lib/session.js`: `login(email, password)` posts `/api/auth/login` with `Origin: BASE_URL` and returns `name=value` for `SESSION_COOKIE` (`sid`; `__Host-sid` in prod-like runs).
- **Mutations** send `Origin`, and approve sends `Idempotency-Key: uuidv4()` (k6 `crypto.randomUUID`).
- **Tags:** every request has `name` (the route template) and `kind` (`simple` or `heavy`).
- The test environment's raised `BFF_RATE_LIMIT_*` and login limits apply (plan §12).

| Script | When | Scenarios | Thresholds | Req |
|---|---|---|---|---|
| `load/smoke.js` | PR (`e2e` job) | `smoke`: `constant-vus` 5 VUs × 30 s, as `LOAD_USER_EMAIL`. Per iteration: `GET /api/auth/me`, `GET /api/employees?limit=50`, `GET /api/pay-runs?limit=20`, `GET /api/pay-runs/{id}` (a seeded run), `GET /api/settings`; `sleep(1)` | `http_req_failed` < 1%; `http_req_duration` p(95) < 300 ms | PAY-NFR-05 |
| `load/full.js` | Nightly | `peak`: `ramping-arrival-rate` 0 → 30 req/s over 1 min, held for 10 min (plan §10: peak 20 × 1.5). The mix: 70% reads (as in smoke); 15% `POST /api/employees` then `PATCH` (unique emails); 10% run lifecycle on a scratch schedule (create, hours, calculate, delete) with `kind:heavy` on calculate; 5% `GET /api/pay-runs/{id}/payouts`. `soak` (`-e SCENARIO=soak`): `constant-arrival-rate` 5 req/s × 30 min, reads plus 10% writes | `http_req_failed` < 1%; `http_req_duration{kind:simple}` p(95) < 300 ms; `http_req_duration{kind:heavy}` p(95) < 1500 ms. Soak also checks for no upward drift: the p95 of the last 5 min is within 20% of the first 5 min, compared in `handleSummary` | PAY-NFR-04, NFR-05 |
| `load/settlement.js` | Nightly (with `seed.py --employees 500`) | `settlement`: `shared-iterations`, 1 VU, 5 iterations. `setup()` signs in as `LOAD_USER_EMAIL` (preparer; payroll_admin) and `approver@payrun.local` (`SEED_APPROVER_PASSWORD`) (Issues #15). Each iteration: find the next unused monthly `period_start` (from `GET /api/pay-runs`); `POST /api/pay-runs`; `GET …/payslips`; `PUT …/hours` for every hourly payslip; `POST …/calculate` (as the preparer, `kind:heavy`); `POST …/approve` as the approver with a fresh `Idempotency-Key` (`kind:heavy`); then poll `GET /api/pay-runs/{id}` every 2 s until `paid` or `partially_failed` (timeout 180 s), recording the Trend `run_settlement_seconds` (from the approve response to the first final status) | `run_settlement_seconds` p(95) < 60 s; `http_req_duration{name:calculate}` and `{name:approve}` p(95) < 1500 ms; `checks` > 99%. Cross-checked against `payroll_payout_settlement_seconds` in Prometheus by the nightly script | PAY-NFR-05, NFR-06 |

## 10. Traceability

| Requirement | Design section(s) | Tests |
|---|---|---|
| PAY-101 Invite | §3.2 users, §6.2 `/users` | `users.routes.test.ts`, `access.spec.ts` |
| PAY-102 Resend | §3.2 invites, §6.2 `/users` | `users.routes.test.ts` |
| PAY-103 Accept | §3.2 auth, §4.1, §6.2 `/accept`, §7.4 span hygiene | `auth.routes.test.ts`, `accept-form.test.tsx`, `access.spec.ts`, `trace.spec.ts` (b) |
| PAY-104 Sign in | §3.2 auth, §3.3, §5.1, §6.2 `/login` | `auth.routes.test.ts`, `login-form.test.tsx`, `access.spec.ts` |
| PAY-105 Reset | §3.2 auth, §4.4, §6.2 `/reset` | `auth.routes.test.ts`, `reset-forms.test.tsx`, `access.spec.ts` |
| PAY-106 Sessions | §2.1, §2.2, §4.1–4.4, §5.5 | `session.test.ts`, `session.int.test.ts`, `require-session.test.ts`, `proxy.test.ts`, `access.spec.ts` |
| PAY-107 Re-check | §4.3 step 5, §4.4 | `require-session.test.ts`, `users.routes.test.ts`, `role-change.spec.ts` |
| PAY-108 User admin | §3.2 users, §6.2 `/users` | `users.routes.test.ts`, `users-page.test.tsx`, `role-change.spec.ts` |
| PAY-109 Auth audit | §3.2 audit, §6.2 `/audit` | `audit.routes.test.ts`, `a11y.spec.ts` |
| PAY-200 Settings | §3.2 settings, §6.2 `/settings` | `settings.routes.test.ts`, `settings-form.test.tsx`, `employees.spec.ts` |
| PAY-201 Create employee | §2.3, §3.2 employees, §6.2 | `employees.routes.test.ts`, `employee-form.test.tsx`, `employees.spec.ts` |
| PAY-202 List and read employees | §2.4 (`Employee` strict), §3.2, §6.2 | `employees.routes.test.ts`, `schemas/*.test.ts`, `employees.spec.ts` |
| PAY-203 Update employee | §3.2, §6.2 `/employees/[id]` | `employees.routes.test.ts`, `employees.spec.ts`, `recover.spec.ts` |
| PAY-300 Create run | §3.2 pay runs, §3.3, §6.2 `/pay-runs/new` | `pay-runs.routes.test.ts`, `pay-run.spec.ts` |
| PAY-301 Hours | §2.3 `HoursBody`, §6.3 | `pay-runs.routes.test.ts`, `hours-editor.test.tsx`, `pay-run.spec.ts` |
| PAY-302 Calculate | §3.2 (10 s), §6.3 | `pay-runs.routes.test.ts`, `pay-run.spec.ts`, `full.js`, `settlement.js` |
| PAY-303 Summary and payslips | §2.4, §3.2, §6.3 | `pay-runs.routes.test.ts`, `pay-run.spec.ts` |
| PAY-304 Delete run | §3.2, §6.3 | `pay-runs.routes.test.ts`, `pay-run.spec.ts` |
| PAY-400 Approve | §3.2 (Idempotency-Key), §6.3 | `pay-runs.routes.test.ts`, `approve-button.test.tsx`, `approve-pay.spec.ts`, `settlement.js` |
| PAY-401 Dual approval | §3.3 (message), §6.3 | `errors.test.ts`, `approve-button.test.tsx`, `approve-pay.spec.ts`, `recover.spec.ts` |
| PAY-413 Live progress | §6.3 polling and badges, §5.1 budget | `run-page.test.tsx`, `payout-badge.test.ts`, `approve-pay.spec.ts` |
| PAY-500 Retry | §3.2, §3.3, §6.3 | `payouts.routes.test.ts`, `retry-button.test.tsx`, `recover.spec.ts` |
| PAY-501 Payout history | §2.4 (`Payout` strict), §6.3 | `payouts.routes.test.ts`, `payouts-tab.test.tsx`, `approve-pay.spec.ts`, `recover.spec.ts` |
| PAY-600 Internal JWT | §3.4.1 | `internal-jwt.test.ts`, `auth.routes.test.ts`, `employees.routes.test.ts` |
| PAY-601 Authorization | §3.1, §4.3, §5.5, §6.1 | `require-session.test.ts`, `proxy.test.ts`, `*.routes.test.ts`, `role-gate.test.tsx`, `employees.spec.ts` |
| PAY-602 Error format | §3.3 | `errors.test.ts`, `*.routes.test.ts`, `errors.spec.ts` |
| PAY-603 Rate limits | §4.5, §5.1, §5.2 | `rate-limit.test.ts`, `rate-limit.int.test.ts`, `client-ip.test.ts`, `auth.routes.test.ts` |
| PAY-604 BFF protections | §5.3–5.6, §7.3 | `body.test.ts`, `origin.test.ts`, `csp.test.ts`, `proxy.test.ts`, `headers.test.ts`, `errors.spec.ts`, CSP listener in `fixtures.ts` |
| PAY-605 Health and startup (web part) | §1.2 `livez`/`readyz`, §8 startup checks | `config.test.ts`, `health.routes.test.ts` |
| PAY-607 Redis ACLs (web) | §2.1 ACL | `session.int.test.ts` |
| PAY-NFR-04 Availability SLO | §7.2 `bff_request_duration_seconds` | `metrics.test.ts`, `full.js` |
| PAY-NFR-05 Latency SLO | §3.4.2, §7.2 | `smoke.js`, `full.js`, `settlement.js` |
| PAY-NFR-06 Settlement SLO | §9.3 | `settlement.js` |
| PAY-NFR-07 No secrets in telemetry | §6.2 (fragment), §7.1 redaction, §7.4, §7.5 | `logger.test.ts`, `telemetry.route.test.ts`, `trace.spec.ts` (b) |
| PAY-NFR-08 Privacy | §2.4 strict schemas, §5.1 hashed subjects, §7.1 | `schemas/*.test.ts`, `employees.routes.test.ts`, `employees.spec.ts` |
| PAY-NFR-09 Tracing | §7.3, §7.4 (`propagateContextUrls`) | `trace.spec.ts` (a), `headers.test.ts` |
| PAY-NFR-10 Observability assets (web metrics) | §7.2 | `metrics.test.ts` |
| PAY-NFR-12 Gates (web) | §9 | CI `web` and `e2e` jobs |
| PAY-NFR-14 Accessibility | §6.3 notes | `a11y.spec.ts` |

## Lead resolutions applied (2026-11-10; README R-table)

These override anything earlier in this file that disagrees.

- **R3:** the generic code is `validation_error`, not `validation_failed`; this file has been updated. Map `payout_not_blocked` to "This payout isn't blocked any more. Refresh the page." and `anchor_locked` to "The biweekly anchor can't change after biweekly runs exist."
- **R8, PAY-502:** new BFF route `POST /api/pay-runs/[id]/payouts/[payoutId]/resolve` (approver, Origin check, Zod `{action: "redrive"|"mark_rejected", note: string 1–500}`) → payroll resolve. On the run page's payouts tab, a "Resolve" button appears only on `blocked` rows and opens a dialog. It says to check the bank first, offers both actions, and requires a note. Vitest: `resolve-route.test.ts`. e2e: a blocked case in `recover.spec.ts`, which forces `needs_operator` through the test-only `mint_token`-authorised internal result call with `poison`.
- **R9:** `internal-jwt.ts` adds the `name` claim for user tokens. Public-flow tokens use `cid = COMPANY_ID`, `roles: []`, and no `name` or `sid`.
- **R19:** after reset confirm, the BFF deletes every session in `web:user-sess:<user_id>` straight away.
- **R21:** the run page shows `calculated_by_name` and `approved_by_name`, reads `counts.blocked` and `counts.awaiting_bank` from the run summary (one poll endpoint), and uses `payslip_id` and `amount_cents` from the payouts list. The users page uses `pending_invite_id` for "Resend invite".
- **R22:** `LOAD_USER_EMAIL` is a payroll_admin; `settlement.js` approves as `approver@payrun.local`.
- **R23, R24:** the HMAC session key and the new config keys are accepted.
- **R36:** the `TRUSTED_PROXY_HOPS=0` spike is part of task S7.

## Issues for the lead (all resolved: see the section above and the spec README R-table)

These are contradictions or gaps in the source documents. The design above uses the stated assumption in each case; none of the sources were edited.

1. **`cid` on `service:web` tokens is undefined.** PAY-600 requires `cid` on every token, but public flows (login, accept, reset) happen before the BFF knows the user. The design uses `cid = COMPANY_ID`; identity's low-level design must accept that, and multi-company would need a different rule.
2. **Identity responses must carry what the session needs.** Plan §7 says login returns "the user and roles". The session (PAY-106) and the re-check (PAY-107) need `id`, `roles`, `roles_version`, `status` and `company_id` from `POST /v1/auth/login`, `POST /v1/invites/accept` and `GET /v1/users/me`.
3. **Reset confirm can't clear `web:user-sess` as written.** Plan §7 says "the BFF clears `web:user-sess`" after `password-reset/confirm`, but the BFF only has the token, not the user ID. The design asks identity's confirm to return `{user_id}`. Without it, revocation falls back to the 60 s `roles_version` re-check (PAY-105 says reset bumps `roles_version`), so earlier sessions get 401 within 60 s rather than at once, which is weaker than PAY-105's "on their next request".
4. **The client IP doesn't reach identity.** identity enforces `AUTH_LOGIN_LIMIT_IP` and records `ip_hash` (PAY-104, PAY-109), but only the BFF sees the client. No header or claim is defined. The design sends `X-Client-IP: <clientIp>`, to be trusted by identity only on `iss=web` tokens. This needs agreement in identity's low-level design.
5. **`TRUSTED_PROXY_HOPS = 0` on Next.js 16.** Route handlers have no socket address (`request.ip` was removed). The design assumes the standalone Next server appends the peer address to `X-Forwarded-For`, so hops 0 means "the right-most entry". This needs a short spike during implementation. If it doesn't hold, a tiny custom server (or a required ingress in every environment) is needed.
6. **Demoted user: 401 or 403?** Plan §12.1's row says "403 within `SESSION_ROLE_RECHECK_S`". PAY-107 and HLD §4 say "401 (session ended), or 403 when the roles shrank but the session was refreshed". The design follows PAY-107: a `roles_version` mismatch always ends the session (401), and 403 follows on re-sign-in.
7. **The invite body differs.** Plan §5 J1 shows `POST /api/users/invite {email, roles}`; PAY-101 has `{email, name, roles[]}`. The design follows PAY-101.
8. **Missing 404 codes.** Only `employee_not_found` and `payout_not_found` (internal endpoint) are named. The design assumes `pay_run_not_found`, `payslip_not_found` and `payout_not_found` (public routes) in payroll, and `user_not_found` and `invite_not_found` in identity. The service low-level designs must confirm them.
9. **No code for generic validation errors.** PAY-602 says "422 with field details" but names no code. The design uses `validation_error` at the BFF, and expects services to use the same. It also adds BFF codes `invalid_json` (400) and `request_failed` (the unknown-4xx fallback).
10. **Resending an invite needs an invite ID** (`POST /v1/invites/{id}/resend`), but `GET /v1/users` (PAY-108) doesn't say it exposes one. The design expects an `invite_id` on pending users.
11. **The payouts list has no amounts.** PAY-501 returns name, status, `no_transfer` and attempts, but no `amount_cents` and no `payslip_id` or `employee_id`. The Payouts tab can't show amounts, or join them to payslips, without one of these. Suggest adding `amount_cents` and `payslip_id` to the payroll response.
12. **The run summary can't show attempt states.** PAY-413 says the run page polls `GET /api/pay-runs/{id}` and shows "awaiting bank" and "blocked", but PAY-303's counts are by payout status (`requested`/`paid`/`failed`), which can't express attempt states. The design polls the payouts list too while `processing`: two requests per 2 s, and about 100 KB+ per poll at 500 payouts. Suggest adding `attempt_counts {indeterminate, needs_operator}` to the run summary.
13. **No display names for `calculated_by` and `approved_by`.** These are user IDs, and only admins can list users (identity `GET /v1/users`). Non-admin roles can't see who calculated or approved a run, which J4 attributability suggests they should. Options: payroll snapshots display names onto the run, or identity offers a name lookup for any role.
14. **The run summary lacks the dual-approval snapshot.** PAY-303 doesn't include the run's `require_dual_approval` snapshot, so the UI can't tell in advance whether the current user may approve. The design shows a soft hint only.
15. **`LOAD_USER_EMAIL` has no stated roles** (PAY-609). The k6 scripts need `payroll_admin` for writes. `settlement.js` also needs an approver who isn't the calculator (dual approval on); the design uses `approver@payrun.local` with `SEED_APPROVER_PASSWORD`. `seed.py` must give `LOAD_USER_EMAIL` at least `payroll_admin`.
16. **Session key form.** PAY-106 says `web:sess:<256-bit id>`. The design stores `web:sess:<HMAC-SHA256(SESSION_SECRET, id)>` (also 256 bits), so a Redis dump yields no usable cookies; this matches the plan's description of `SESSION_SECRET` as a "session hashing salt". Please confirm the deviation.
17. **New configuration keys and one behaviour choice.** The design adds keys not in plan §2.2:
    - `SESSION_RECHECK_GRACE_S` (300);
    - `SESSION_TOUCH_INTERVAL_S` (60);
    - `BFF_TELEMETRY_RATE_LIMIT_IP` (60);
    - `BFF_BODY_LIMIT_BYTES`;
    - `UPSTREAM_*` timeouts and retries.

    The grace window is a design choice. Plan §12.1 wants signed-in journeys to keep working while identity is down, but PAY-107's "within 60 s" can't hold without identity. The design fails open for up to 300 s after the last successful check, then returns 503. Please confirm.
18. **The invite and reset links depend on identity.** identity builds the email links, so it needs the web's public URL (no identity config key is named for it). The paths must be `/accept#token=…` and `/reset#token=…` for the pages in §6.2.
19. **The payroll audit response is unspecified.** PAY-109 specifies identity's audit fields; nothing specifies `GET /v1/audit-events` (fields, cursor). The design assumes `{id, at, actor_user_id, event, entity_type, entity_id, changes}`.
20. **Pagination envelope.** No document fixes the list shape. The design assumes `{items, next_cursor}` for every cursor list, and `{items}` for payslips and payouts.
21. **Session limits barely fail open in practice.** PAY-603 says per-session limits "fail open" when Redis is down, but the session lookup itself needs Redis and returns 503 (plan §2.1). So the fail-open only matters for flapping connections and telemetry. This isn't a contradiction, but the wording may mislead a reader into expecting signed-in requests to keep working during a Redis outage.
