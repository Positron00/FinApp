"""Email rendering (identity LLD §3.9; PAY-103, PAY-105, PAY-110, PAY-NFR-07).

The token travels only in the URL fragment, never in a query string. Rendered text, the link and
the raw token are never stored or logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Final, Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

__all__ = ["SUBJECTS", "RenderedEmail", "Template", "build_message", "link_for", "render"]

Template = Literal["invite", "password_reset"]

SUBJECTS: Final[dict[str, str]] = {
    "invite": "You're invited to PayRun",
    "password_reset": "Reset your PayRun password",
}
_PATHS: Final[dict[str, str]] = {"invite": "accept", "password_reset": "reset"}

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=True),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str = field(repr=False)
    html: str = field(repr=False)


def link_for(template: Template, raw_token: str, base_url: str) -> str:
    """``{base}/accept#token=…`` or ``{base}/reset#token=…`` (fragment, never a query string)."""
    return f"{base_url.rstrip('/')}/{_PATHS[template]}#token={raw_token}"


def render(template: Template, *, name: str, link: str, expires_at: datetime) -> RenderedEmail:
    expires_at_utc = expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    values = {"name": name, "link": link, "expires_at_utc": expires_at_utc}
    return RenderedEmail(
        subject=SUBJECTS[template],
        text=_env.get_template(f"{template}.txt.j2").render(**values),
        html=_env.get_template(f"{template}.html.j2").render(**values),
    )


def build_message(rendered: RenderedEmail, *, sender: str, recipient: str) -> EmailMessage:
    """``multipart/alternative`` (text + HTML) with the §3.9 headers."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = rendered.subject
    msg["Auto-Submitted"] = "auto-generated"
    msg["Message-ID"] = make_msgid(domain=_domain_of(sender))
    msg.set_content(rendered.text)
    msg.add_alternative(rendered.html, subtype="html")
    return msg


def _domain_of(address: str) -> str:
    _, at, domain = address.rpartition("@")
    return (domain.strip(" >") if at else "") or "payrun.example.com"
