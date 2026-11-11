"""Seed the running dev stack (platform LLD §3.15; PAY-609, C27, C33, C54, R16, R22).

    uv run python scripts/seed.py --skeleton [--base-url http://127.0.0.1:${WEB_PORT}] [--out PATH]

Progress goes to stderr; stdout carries only the JSON summary::

    {"mode": "skeleton", "company_id": "...", "users": {"owner": ..., "approver": ..., "load": ...},
     "employees": 5, "runs": {"current": "...", "draft": "..."}}

A re-run prints the same IDs and counts. Exit codes: 0 done; 1 an API step failed (the step, the
HTTP status and the error ``code`` are logged, never a body); 2 arguments or ``ENV``; 3 the stack
was not ready within 60 s.

**Routes.** Every step goes through the BFF and Mailpit when the BFF has the route. The skeleton
BFF (task S7) has sign-in and the pay-run routes only; user invites, invite acceptance and
employees arrive with task F5. Until then those steps call identity and payroll directly on their
test-profile ports (``IDENTITY_PORT``, ``PAYROLL_PORT``; ``compose.test.yaml``) with tokens from
``scripts/mint_token.py``, exactly as the BFF would: the owner's user token for invite and
employee writes, a ``service:web`` token for accept. The BFF route is tried first and used as soon
as it exists (any answer but 404 ``not_found``), so this file needs no change at F5.

The full seed (``--employees 10|500``: hourly employees, both schedules, settings PUT, hours) needs
payroll's hours and settings work from F2/F3; until then it exits 2 with a message.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, NoReturn
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for scripts.*
from scripts.mint_token import EnvError, load_env, mint

READY_TIMEOUT_S: Final = 60.0
HTTP_TIMEOUT_S: Final = 20.0
APPROVER_EMAIL: Final = "approver@payrun.example.com"
OWNER_ROLES: Final = ("admin", "approver", "payroll_admin")
SKELETON_EMPLOYEES: Final = 5
SEED: Final = 609  # fixed, so names, salaries and bank numbers are reproducible


class StepError(Exception):
    """An API step failed: exit 1 after logging the step, the status and the error code."""

    def __init__(self, step: str, status: int | None, code: str | None) -> None:
        super().__init__(step)
        self.step, self.status, self.code = step, status, code


def log(msg: str, **fields: object) -> None:
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    sys.stderr.write(f"[seed] {msg}{' ' + extra if extra else ''}\n")


def fail(code: int, msg: str) -> NoReturn:
    log(msg)
    raise SystemExit(code)


def _error_code(r: httpx.Response) -> str | None:
    try:
        body = r.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            return str(err["code"])
        if isinstance(body.get("code"), str):
            return str(body["code"])
    return None


def _check(step: str, r: httpx.Response, ok: tuple[int, ...] = (200, 201)) -> Any:
    if r.status_code not in ok:
        raise StepError(step, r.status_code, _error_code(r))
    return r.json() if r.content else None


@dataclass
class Ctx:
    env: Mapping[str, str]
    base_url: str
    mailpit_url: str
    identity_url: str
    payroll_url: str
    http: httpx.Client
    cookies: dict[str, str] = field(default_factory=dict)  # email -> "sid=<value>"
    bff_missing: set[str] = field(default_factory=set)

    # ---- BFF -------------------------------------------------------------------------------
    def bff(
        self, method: str, path: str, *, as_email: str | None = None, **kw: Any
    ) -> httpx.Response:
        headers = {"Origin": self.base_url, **kw.pop("headers", {})}
        if as_email is not None:
            headers["Cookie"] = self.cookies[as_email]
        return self.http.request(method, self.base_url + path, headers=headers, **kw)

    def bff_has(self, key: str, r: httpx.Response) -> bool:
        """False (and remembered) when the BFF answers 404 not_found: the route isn't built yet."""
        if r.status_code == 404 and _error_code(r) == "not_found":
            self.bff_missing.add(key)
            return False
        return True

    def login(self, email: str, password: str) -> dict[str, Any] | None:
        r = self.bff("POST", "/api/auth/login", json={"email": email, "password": password})
        if r.status_code == 401:
            return None
        body = _check(f"login {email}", r)
        name = self.env.get("SESSION_COOKIE_NAME", "sid")
        sid = r.cookies.get(name)
        if sid is None:  # Secure cookies over http: read the header ourselves
            for h in r.headers.get_list("set-cookie"):
                if h.startswith(f"{name}="):
                    sid = h.split(";", 1)[0].split("=", 1)[1]
        if not sid:
            raise StepError(f"login {email}", r.status_code, "no_session_cookie")
        self.cookies[email] = f"{name}={sid}"
        user = body["user"]
        return dict(user)

    # ---- direct service calls (until the F5 BFF routes exist) ------------------------------
    def owner_token(self, aud: str, owner_id: str) -> str:
        return mint(aud, owner_id, OWNER_ROLES, name="Owner", env=self.env)

    def service(self, method: str, url: str, token: str, **kw: Any) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": str(uuid.uuid4()),
            **kw.pop("headers", {}),
        }
        return self.http.request(method, url, headers=headers, **kw)


# ------------------------------------------------------------------------------------------------
def wait_ready(ctx: Ctx) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    targets = {
        "web": f"{ctx.base_url}/api/readyz",
        "mailpit": f"{ctx.mailpit_url}/api/v1/info",
    }
    pending = dict(targets)
    while pending:
        for name, url in list(pending.items()):
            try:
                if ctx.http.get(url, timeout=3).status_code == 200:
                    pending.pop(name)
            except httpx.HTTPError:
                pass
        if not pending:
            break
        if time.monotonic() > deadline:
            fail(3, f"stack not ready after {READY_TIMEOUT_S:.0f}s: {', '.join(pending)}")
        time.sleep(1)
    log("stack ready")


def mailpit_token(ctx: Ctx, email: str, kind: str = "accept", timeout_s: float = 15.0) -> str:
    """The newest ``/<kind>#token=…`` link sent to ``email``."""
    import re  # noqa: PLC0415

    pattern = re.compile(rf"/{kind}#token=([A-Za-z0-9_-]+)")
    deadline = time.monotonic() + timeout_s
    query = quote(f'to:"{email}"')
    while True:
        r = ctx.http.get(f"{ctx.mailpit_url}/api/v1/search?query={query}&limit=10")
        msgs = _check("mailpit search", r).get("messages") or []
        for m in msgs:  # newest first
            detail = _check(
                "mailpit message", ctx.http.get(f"{ctx.mailpit_url}/api/v1/message/{m['ID']}")
            )
            found = pattern.search(detail.get("Text") or "") or pattern.search(
                detail.get("HTML") or ""
            )
            if found:
                return found.group(1)
        if time.monotonic() > deadline:
            raise StepError(f"mailpit {kind} email for {email}", None, "email_not_found")
        time.sleep(0.25)


def ensure_user(
    ctx: Ctx, *, owner_id: str, email: str, name: str, roles: list[str], password: str
) -> str:
    """Invite (if new), accept from the Mailpit link, sign in; returns the user id (C27)."""
    step = f"invite {email}"
    r = ctx.bff(
        "POST",
        "/api/users/invite",
        as_email=ctx.env["SEED_OWNER_EMAIL"],
        json={"email": email, "name": name, "roles": roles},
    )
    if not ctx.bff_has("invite", r):
        r = ctx.service(
            "POST",
            f"{ctx.identity_url}/v1/users/invite",
            ctx.owner_token("identity", owner_id),
            json={"email": email, "name": name, "roles": roles},
        )
    if r.status_code in (200, 201):
        created = True
    elif r.status_code == 409:
        created = False  # exists: active (skip) or pending (accept the newest invite below)
    else:
        raise StepError(step, r.status_code, _error_code(r))

    if not created:
        user = ctx.login(email, password)
        if user is not None:
            log("user exists", email=email)
            return str(user["id"])

    token = mailpit_token(ctx, email)
    body = {"token": token, "password": password}
    r = ctx.bff("POST", "/api/auth/accept", json=body)
    if not ctx.bff_has("accept", r):
        r = ctx.service(
            "POST",
            f"{ctx.identity_url}/v1/invites/accept",
            mint("identity", "service:web", [], env=ctx.env),
            headers={"X-Client-IP": "127.0.0.1"},
            json=body,
        )
    _check(f"accept {email}", r)
    user = ctx.login(email, password)
    if user is None:
        raise StepError(f"login {email}", 401, "invalid_credentials")
    log("user created", email=email, roles=",".join(roles))
    return str(user["id"])


def _pages(fetch: Callable[[str | None], httpx.Response], step: str) -> Iterator[dict[str, Any]]:
    cursor: str | None = None
    while True:
        page = _check(step, fetch(cursor))
        yield from page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            return


def list_employees(ctx: Ctx, owner_email: str, owner_id: str) -> list[dict[str, Any]]:
    def via_bff(cursor: str | None) -> httpx.Response:
        params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
        return ctx.bff("GET", "/api/employees", as_email=owner_email, params=params)

    probe = via_bff(None)
    if ctx.bff_has("employees", probe):
        return list(_pages(via_bff, "list employees"))

    def direct(cursor: str | None) -> httpx.Response:
        params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
        return ctx.service(
            "GET",
            f"{ctx.payroll_url}/v1/employees",
            ctx.owner_token("payroll", owner_id),
            params=params,
        )

    return list(_pages(direct, "list employees"))


def employee_payload(i: int) -> dict[str, Any]:
    """Reproducible salaried monthly employee ``i`` (1-based). No account ends in 0000 (that's
    the bank-stub's closed-account trigger, used by the full seed only)."""
    rnd = random.Random(SEED * 1000 + i)  # noqa: S311  (reproducible fake data, not crypto)
    first = [
        "Ada",
        "Grace",
        "Alan",
        "Edsger",
        "Barbara",
        "Donald",
        "Frances",
        "Ken",
        "Radia",
        "Tim",
    ]
    last = ["Lovelace", "Hopper", "Turing", "Dijkstra", "Liskov", "Knuth", "Allen", "Thompson"]
    account = f"{rnd.randrange(10**9, 10**10)}"
    if account.endswith("0000"):
        account = account[:-1] + "1"
    return {
        "name": f"{first[(i - 1) % len(first)]} {last[(i * 3) % len(last)]}",
        "email": f"employee{i:03d}@payrun.example.com",
        "pay_type": "salaried",
        "schedule": "monthly",
        "annual_salary_cents": rnd.randrange(48_000, 120_000) * 100,
        "bank_routing": "021000021",
        "bank_account": account,
    }


def ensure_employees(ctx: Ctx, owner_email: str, owner_id: str, target: int) -> int:
    existing = list_employees(ctx, owner_email, owner_id)
    emails = {e["email"] for e in existing}
    created = 0
    for i in range(1, target + 1):
        body = employee_payload(i)
        if body["email"] in emails:
            continue
        r = ctx.bff("POST", "/api/employees", as_email=owner_email, json=body)
        if not ctx.bff_has("employees", r):
            r = ctx.service(
                "POST",
                f"{ctx.payroll_url}/v1/employees",
                ctx.owner_token("payroll", owner_id),
                json=body,
            )
        _check(f"create employee {i}", r)
        created += 1
    total = len(existing) + created
    log("employees", existing=len(existing), created=created, total=total)
    return total


def month_start(today: date, offset: int = 0) -> date:
    y, m = divmod(today.year * 12 + (today.month - 1) + offset, 12)
    return date(y, m + 1, 1)


def ensure_runs(ctx: Ctx, owner_email: str) -> dict[str, str]:
    def page(cursor: str | None) -> httpx.Response:
        params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
        return ctx.bff("GET", "/api/pay-runs", as_email=owner_email, params=params)

    runs = list(_pages(page, "list pay runs"))
    today = datetime.now(UTC).date()
    out: dict[str, str] = {}
    for label, start in (("current", month_start(today)), ("draft", month_start(today, 1))):
        run = next(
            (
                x
                for x in runs
                if x["schedule"] == "monthly" and x["period_start"] == start.isoformat()
            ),
            None,
        )
        if run is None:
            r = ctx.bff(
                "POST",
                "/api/pay-runs",
                as_email=owner_email,
                json={"schedule": "monthly", "period_start": start.isoformat()},
            )
            run = _check(f"create {label} run", r, ok=(201,))
            log("run created", which=label, period_start=start.isoformat())
        if label == "current" and run["status"] == "draft":
            r = ctx.bff("POST", f"/api/pay-runs/{run['id']}/calculate", as_email=owner_email)
            run = _check("calculate current run", r)
            log("run calculated", which=label)
        out[label] = str(run["id"])
    return out


# ------------------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    class P(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            fail(2, f"arguments: {message}")

    p = P(description="Seed the dev stack through the BFF and Mailpit (PAY-609).")
    p.add_argument(
        "--skeleton", action="store_true", help="owner, approver, load user, 5 employees"
    )
    p.add_argument("--employees", type=int, choices=[10, 500], default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args(argv)
    if a.skeleton and a.employees is not None:
        fail(2, "arguments: --skeleton and --employees are exclusive")
    return a


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    env = load_env()
    if env.get("ENV", "dev") != "dev":
        fail(2, "refusing to seed: ENV is not dev (PAY-609)")
    if not a.skeleton:
        fail(
            2, "the full seed (hourly employees, settings, hours) needs tasks F2/F3; use --skeleton"
        )
    missing = [
        k
        for k in (
            "SEED_OWNER_EMAIL",
            "SEED_OWNER_PASSWORD",
            "SEED_APPROVER_PASSWORD",
            "LOAD_USER_EMAIL",
            "LOAD_USER_PASSWORD",
            "WEB_PORT",
            "MAILPIT_PORT",
        )
        if not env.get(k)
    ]
    if missing:
        fail(2, f"missing settings: {', '.join(missing)} (run scripts/dev-env.sh)")

    base = (a.base_url or f"http://127.0.0.1:{env['WEB_PORT']}").rstrip("/")
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as http:
        ctx = Ctx(
            env=env,
            base_url=base,
            mailpit_url=f"http://127.0.0.1:{env['MAILPIT_PORT']}",
            identity_url=f"http://127.0.0.1:{env.get('IDENTITY_PORT', '4110')}",
            payroll_url=f"http://127.0.0.1:{env.get('PAYROLL_PORT', '4111')}",
            http=http,
        )
        wait_ready(ctx)
        try:
            owner_email = env["SEED_OWNER_EMAIL"]
            owner = ctx.login(owner_email, env["SEED_OWNER_PASSWORD"])
            if owner is None:
                raise StepError("login owner", 401, "invalid_credentials")
            owner_id = str(owner["id"])
            approver_id = ensure_user(
                ctx,
                owner_id=owner_id,
                email=APPROVER_EMAIL,
                name="Seed Approver",
                roles=["approver"],
                password=env["SEED_APPROVER_PASSWORD"],
            )
            load_id = ensure_user(
                ctx,
                owner_id=owner_id,
                email=env["LOAD_USER_EMAIL"],
                name="Load User",
                roles=["payroll_admin"],
                password=env["LOAD_USER_PASSWORD"],
            )
            total = ensure_employees(ctx, owner_email, owner_id, SKELETON_EMPLOYEES)
            runs = ensure_runs(ctx, owner_email)
        except StepError as exc:
            log("step failed", step=exc.step, status=exc.status, code=exc.code)
            return 1
        except (EnvError, ValueError) as exc:
            log("configuration error", error=type(exc).__name__)
            return 2
        except httpx.HTTPError as exc:
            log("transport error", error=type(exc).__name__)
            return 1
        if ctx.bff_missing:
            log(
                "BFF routes not built yet, called the service directly",
                routes=",".join(sorted(ctx.bff_missing)),
            )

    summary = {
        "mode": "skeleton",
        "company_id": env.get("COMPANY_ID"),
        "users": {"owner": owner_id, "approver": approver_id, "load": load_id},
        "employees": total,
        "runs": runs,
    }
    text = json.dumps(summary, indent=2)
    if a.out is not None:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
