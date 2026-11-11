"""Seed the running dev stack (platform LLD §3.15; PAY-609, C27, C33, C54, R16, R22).

    uv run python scripts/seed.py [--skeleton | --employees 10|500]
        [--base-url http://127.0.0.1:${WEB_PORT}] [--out PATH]

Progress goes to stderr; stdout carries only the JSON summary::

    {"mode": "full", "company_id": "...", "users": {"owner": ..., "approver": ..., "load": ...},
     "employees": 10, "runs": {"current": "...", "draft": "..."}}

Modes:

- ``--skeleton`` (C33): 5 salaried monthly employees, no settings PUT (the migration's 0/0 rates).
- full (the default, ``--employees 10``): settings tax 2000 bps, retirement 500 bps and dual
  approval on (R22); 10 employees, salaried and hourly on both schedules, employee 9 (biweekly)
  with an account ending ``0000`` (the bank-stub's closed-account trigger, used by J4 and J5).
  Employees 1 to 5 are the skeleton's, so a skeleton stack can be upgraded in place.
- ``--employees 500``: the full set plus monthly employees up to 500 in total (every 10th
  hourly), for k6. No account outside employee 9 ends in ``0000``.

Both modes make sure a calculated monthly run for the current month (160 hours for each hourly
payslip) and a draft monthly run for the next month exist (C33, C54). A run already past
``calculated`` is left as it is.

A re-run is a no-op and prints the same IDs and counts (PAY-609): users are found by signing in,
employees by email, runs by schedule and period, and the settings are written only when they
differ. Exit codes: 0 done; 1 an API step failed (the step, the HTTP status and the error
``code`` are logged, never a body); 2 arguments or ``ENV``; 3 the stack was not ready within 60 s.

Every step goes through the BFF and Mailpit, like a user would (no SQL, no service tokens).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, NoReturn
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for scripts.*
from scripts.mint_token import EnvError, load_env

READY_TIMEOUT_S: Final = 60.0
HTTP_TIMEOUT_S: Final = 20.0
APPROVER_EMAIL: Final = "approver@payrun.example.com"
SKELETON_EMPLOYEES: Final = 5
FULL_EMPLOYEES: Final = 10
SEED: Final = 609  # fixed, so names, salaries and bank numbers are reproducible
CLOSED_ACCOUNT_EMPLOYEE: Final = 9  # its account ends in 0000 (bank-stub: account_closed)
SEED_HOURS_HUNDREDTHS: Final = 16_000  # 160 h for each hourly payslip of the current run
HOURS_BATCH: Final = 500  # PAY-301: 1 to 500 entries per PUT
FULL_SETTINGS: Final[dict[str, Any]] = {
    "tax_rate_bps": 2000,
    "retirement_bps": 500,
    "require_dual_approval": True,
}

# Employees 6 to 10 of the full set (1 to 5 are the skeleton's salaried monthly ones).
FULL_SHAPES: Final[dict[int, tuple[str, str]]] = {
    6: ("hourly", "monthly"),
    7: ("salaried", "biweekly"),
    8: ("hourly", "biweekly"),
    CLOSED_ACCOUNT_EMPLOYEE: ("salaried", "biweekly"),
    10: ("hourly", "biweekly"),
}


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
    http: httpx.Client
    cookies: dict[str, str] = field(default_factory=dict)  # email -> "sid=<value>"

    def bff(
        self, method: str, path: str, *, as_email: str | None = None, **kw: Any
    ) -> httpx.Response:
        headers = {"Origin": self.base_url, **kw.pop("headers", {})}
        if as_email is not None:
            headers["Cookie"] = self.cookies[as_email]
        return self.http.request(method, self.base_url + path, headers=headers, **kw)

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
        return dict(body["user"])


# ------------------------------------------------------------------------------------------------
def wait_ready(ctx: Ctx) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    pending = {"web": f"{ctx.base_url}/api/readyz", "mailpit": f"{ctx.mailpit_url}/api/v1/info"}
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


def _pages(fetch: Callable[[str | None], httpx.Response], step: str) -> Iterator[dict[str, Any]]:
    """Every item of a cursor-paged list, following ``next_cursor`` until it is null (C8)."""
    cursor: str | None = None
    while True:
        page = _check(step, fetch(cursor))
        yield from page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            return


def list_all(ctx: Ctx, as_email: str, path: str) -> list[dict[str, Any]]:
    def page(cursor: str | None) -> httpx.Response:
        params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
        return ctx.bff("GET", path, as_email=as_email, params=params)

    return list(_pages(page, f"list {path}"))


def ensure_user(ctx: Ctx, owner_email: str, *, email: str, name: str, roles: list[str]) -> str:
    """Sign in if the user exists; otherwise invite (or resend a pending invite), accept the
    Mailpit link and sign in. Returns the user id (C27)."""
    password = (
        ctx.env["SEED_APPROVER_PASSWORD"]
        if email == APPROVER_EMAIL
        else ctx.env["LOAD_USER_PASSWORD"]
    )
    user = ctx.login(email, password)
    if user is not None:
        log("user exists", email=email)
        return str(user["id"])

    r = ctx.bff(
        "POST",
        "/api/users/invite",
        as_email=owner_email,
        json={"email": email, "name": name, "roles": roles},
    )
    if r.status_code == 409:  # pending (an earlier run stopped before accepting): resend
        users = list_all(ctx, owner_email, "/api/users")
        pending = next((u for u in users if u["email"] == email), None)
        if pending is None or not pending.get("pending_invite_id"):
            raise StepError(f"invite {email}", 409, "user_email_taken")
        r = ctx.bff(
            "POST", f"/api/invites/{pending['pending_invite_id']}/resend", as_email=owner_email
        )
        _check(f"resend invite {email}", r)
    else:
        _check(f"invite {email}", r)

    token = mailpit_token(ctx, email)
    _check(
        f"accept {email}",
        ctx.bff("POST", "/api/auth/accept", json={"token": token, "password": password}),
    )
    user = ctx.login(email, password)
    if user is None:
        raise StepError(f"login {email}", 401, "invalid_credentials")
    log("user created", email=email, roles=",".join(roles))
    return str(user["id"])


def ensure_settings(ctx: Ctx, owner_email: str) -> None:
    """Full mode: tax 2000 bps, retirement 500 bps, dual approval on (R22). PUT only on a diff."""
    current = _check("get settings", ctx.bff("GET", "/api/settings", as_email=owner_email))
    if all(current.get(k) == v for k, v in FULL_SETTINGS.items()):
        log("settings already set")
        return
    body = {
        "name": current["name"],
        "timezone": current["timezone"],
        "biweekly_anchor": current["biweekly_anchor"],
        **FULL_SETTINGS,
    }
    _check("put settings", ctx.bff("PUT", "/api/settings", as_email=owner_email, json=body))
    log("settings set", **FULL_SETTINGS)


def _shape(i: int, mode: str) -> tuple[str, str]:
    """(pay_type, schedule) of employee ``i``."""
    if mode == "skeleton" or i <= SKELETON_EMPLOYEES:
        return ("salaried", "monthly")
    if i in FULL_SHAPES:
        return FULL_SHAPES[i]
    return ("hourly" if i % 10 == 0 else "salaried", "monthly")


def employee_payload(i: int, mode: str = "skeleton") -> dict[str, Any]:
    """Reproducible employee ``i`` (1-based)."""
    rnd = random.Random(SEED * 1000 + i)  # noqa: S311  (reproducible fake data, not crypto)
    first = [
        *("Ada", "Grace", "Alan", "Edsger", "Barbara"),
        *("Donald", "Frances", "Ken", "Radia", "Tim"),
    ]
    last = ["Lovelace", "Hopper", "Turing", "Dijkstra", "Liskov", "Knuth", "Allen", "Thompson"]
    account = f"{rnd.randrange(10**9, 10**10)}"
    if account.endswith("0000"):
        account = account[:-1] + "1"
    pay_type, schedule = _shape(i, mode)
    if mode != "skeleton" and i == CLOSED_ACCOUNT_EMPLOYEE:
        account = account[:-4] + "0000"
    body: dict[str, Any] = {
        "name": f"{first[(i - 1) % len(first)]} {last[(i * 3) % len(last)]}",
        "email": f"employee{i:03d}@payrun.example.com",
        "pay_type": pay_type,
        "schedule": schedule,
        "bank_routing": "021000021",
        "bank_account": account,
    }
    if pay_type == "salaried":
        body["annual_salary_cents"] = rnd.randrange(48_000, 120_000) * 100
    else:
        body["hourly_rate_cents"] = rnd.randrange(18, 60) * 100 + rnd.choice((0, 25, 50, 75))
    return body


def ensure_employees(ctx: Ctx, owner_email: str, target: int, mode: str) -> int:
    existing = list_all(ctx, owner_email, "/api/employees")
    emails = {e["email"] for e in existing}
    created = 0
    for i in range(1, target + 1):
        body = employee_payload(i, mode)
        if body["email"] in emails:
            continue
        r = ctx.bff("POST", "/api/employees", as_email=owner_email, json=body)
        if r.status_code == 409 and _error_code(r) == "employee_email_taken":
            continue  # created by a concurrent or interrupted run
        _check(f"create employee {i}", r, ok=(201,))
        created += 1
        if created % 100 == 0:
            log("employees", created=created)
    total = len(existing) + created
    log("employees", existing=len(existing), created=created, total=total)
    return total


def month_start(today: date, offset: int = 0) -> date:
    y, m = divmod(today.year * 12 + (today.month - 1) + offset, 12)
    return date(y, m + 1, 1)


def enter_hours(ctx: Ctx, owner_email: str, run_id: str) -> int:
    """160 h for each hourly payslip without hours, in PUTs of at most 500 (PAY-301)."""
    slips = list_all(ctx, owner_email, f"/api/pay-runs/{run_id}/payslips")
    entries = [
        {"employee_id": p["employee_id"], "hours_hundredths": SEED_HOURS_HUNDREDTHS}
        for p in slips
        if p["pay_type"] == "hourly" and p.get("hours_hundredths") is None
    ]
    for i in range(0, len(entries), HOURS_BATCH):
        batch = entries[i : i + HOURS_BATCH]
        r = ctx.bff(
            "PUT", f"/api/pay-runs/{run_id}/hours", as_email=owner_email, json={"entries": batch}
        )
        _check("enter hours", r)
    return len(entries)


def ensure_runs(ctx: Ctx, owner_email: str) -> dict[str, str]:
    runs = list_all(ctx, owner_email, "/api/pay-runs")
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
            hours = enter_hours(ctx, owner_email, str(run["id"]))
            r = ctx.bff("POST", f"/api/pay-runs/{run['id']}/calculate", as_email=owner_email)
            run = _check("calculate current run", r)
            log("run calculated", which=label, hourly_entries=hours)
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


REQUIRED: Final = (
    "SEED_OWNER_EMAIL",
    "SEED_OWNER_PASSWORD",
    "SEED_APPROVER_PASSWORD",
    "LOAD_USER_EMAIL",
    "LOAD_USER_PASSWORD",
    "WEB_PORT",
    "MAILPIT_PORT",
)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    env = load_env()
    if env.get("ENV", "dev") != "dev":
        fail(2, "refusing to seed: ENV is not dev (PAY-609)")
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        fail(2, f"missing settings: {', '.join(missing)} (run scripts/dev-env.sh)")
    mode = "skeleton" if a.skeleton else "full"
    target = SKELETON_EMPLOYEES if a.skeleton else (a.employees or FULL_EMPLOYEES)

    base = (a.base_url or f"http://127.0.0.1:{env['WEB_PORT']}").rstrip("/")
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as http:
        ctx = Ctx(
            env=env,
            base_url=base,
            mailpit_url=f"http://127.0.0.1:{env['MAILPIT_PORT']}",
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
                ctx, owner_email, email=APPROVER_EMAIL, name="Seed Approver", roles=["approver"]
            )
            load_id = ensure_user(
                ctx,
                owner_email,
                email=env["LOAD_USER_EMAIL"],
                name="Load User",
                roles=["payroll_admin"],
            )
            if mode == "full":
                ensure_settings(ctx, owner_email)
            total = ensure_employees(ctx, owner_email, target, mode)
            runs = ensure_runs(ctx, owner_email)
        except StepError as exc:
            log("step failed", step=exc.step, status=exc.status, code=exc.code)
            return 1
        except (EnvError, ValueError, KeyError) as exc:
            log("configuration error", error=type(exc).__name__)
            return 2
        except httpx.HTTPError as exc:
            log("transport error", error=type(exc).__name__)
            return 1

    summary = {
        "mode": mode,
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
