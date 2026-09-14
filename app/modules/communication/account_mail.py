"""The mail an account's own lifecycle sends: invitations, passwords, resets.

Separate from the broadcast notifications because these are never optional and
never batched — a person is locked out until one of them arrives. Each one
swallows its own failure: a mail server having a bad day must not turn creating
a user into a 500, and the delivery attempt is logged either way.
"""

import logging
from urllib.parse import quote

from app.core.config import settings
from app.core.context import TenantContext
from app.modules.communication.notify import _send_email

log = logging.getLogger("scholarly.account_mail")


def _link(path: str) -> str:
    return f"{settings.web_app_url.rstrip('/')}{path}"


def _wrap(heading: str, lines: list[str], button: tuple[str, str] | None = None) -> str:
    """Plain, table-free HTML — it renders the same in Gmail and in Outlook."""
    body = "".join(f"<p style='margin:0 0 12px'>{line}</p>" for line in lines)
    action = (
        f"<p style='margin:24px 0'><a href='{button[1]}' "
        "style='background:#111214;color:#fff;text-decoration:none;padding:12px 20px;"
        f"border-radius:10px;display:inline-block;font-weight:600'>{button[0]}</a></p>"
        if button else ""
    )
    return (
        "<div style='font-family:system-ui,-apple-system,Segoe UI,sans-serif;"
        "font-size:15px;line-height:1.55;color:#111214;max-width:520px'>"
        f"<h2 style='margin:0 0 16px;font-size:20px'>{heading}</h2>"
        f"{body}{action}"
        "<p style='margin:24px 0 0;font-size:13px;color:#6b6f76'>"
        f"{settings.app_name}</p></div>"
    )


async def send_invite(
    *, to: str, full_name: str, tenant: TenantContext, token: str, temporary_password: str
) -> None:
    """Invitation with a one-time password and the link that activates it."""
    link = _link(f"/accept-invite?token={token}")
    first = (full_name or "there").split()[0]
    try:
        await _send_email(
            to,
            f"Your {tenant.name} account",
            (
                f"Hello {first},\n\n"
                f"An account has been created for you at {tenant.name}.\n\n"
                f"Sign in with: {to}\n"
                f"Temporary password: {temporary_password}\n\n"
                f"Open this link to set your own password:\n{link}\n\n"
                "The link is good for seven days. If you were not expecting this, "
                "you can ignore it."
            ),
            html=_wrap(
                f"Your {tenant.name} account",
                [
                    f"Hello {first},",
                    f"An account has been created for you at <b>{tenant.name}</b>.",
                    f"Sign in with <b>{to}</b> and this temporary password:",
                    f"<code style='background:#f4f4f5;padding:6px 10px;border-radius:6px;"
                    f"font-size:16px;letter-spacing:0.5px'>{temporary_password}</code>",
                    "You will be asked to choose your own password straight away.",
                    "<span style='color:#6b6f76;font-size:13px'>The link is good for seven "
                    "days. If you were not expecting this, you can ignore it.</span>",
                ],
                button=("Set your password", link),
            ),
        )
    except Exception:
        log.exception("Could not send the invitation to %s", to)


async def send_temporary_password(
    *, to: str, full_name: str, tenant: TenantContext, temporary_password: str
) -> None:
    """After an administrator resets someone's password for them."""
    first = (full_name or "there").split()[0]
    try:
        await _send_email(
            to,
            f"Your {tenant.name} password was reset",
            (
                f"Hello {first},\n\n"
                f"An administrator at {tenant.name} has reset your password.\n\n"
                f"Temporary password: {temporary_password}\n\n"
                f"Sign in at {_link('/login')} and you will be asked to choose a new one.\n\n"
                "If this was not expected, tell the school office — someone has "
                "access to your account settings."
            ),
            html=_wrap(
                "Your password was reset",
                [
                    f"Hello {first},",
                    f"An administrator at <b>{tenant.name}</b> has reset your password.",
                    "Your temporary password is:",
                    f"<code style='background:#f4f4f5;padding:6px 10px;border-radius:6px;"
                    f"font-size:16px;letter-spacing:0.5px'>{temporary_password}</code>",
                    "You will be asked to choose a new one as soon as you sign in.",
                    "<span style='color:#6b6f76;font-size:13px'>If this was not expected, "
                    "tell the school office.</span>",
                ],
                button=("Sign in", _link(f"/login?email={quote(to)}")),
            ),
        )
    except Exception:
        log.exception("Could not send the temporary password to %s", to)


async def send_password_reset(*, to: str, token: str) -> None:
    """Self-service reset. Never raises — an exception escaping here would
    itself disclose that the address has an account."""
    link = _link(f"/reset-password?token={token}")
    try:
        await _send_email(
            to,
            "Reset your password",
            (
                "Someone asked to reset the password on your account.\n\n"
                f"Choose a new one here — the link is good for one hour:\n{link}\n\n"
                "If that was not you, nothing has changed and you can ignore this."
            ),
            html=_wrap(
                "Reset your password",
                [
                    "Someone asked to reset the password on your account.",
                    "The link below is good for one hour.",
                    "<span style='color:#6b6f76;font-size:13px'>If that was not you, "
                    "nothing has changed and you can ignore this.</span>",
                ],
                button=("Choose a new password", link),
            ),
        )
    except Exception:
        log.exception("Could not send the password reset mail")
