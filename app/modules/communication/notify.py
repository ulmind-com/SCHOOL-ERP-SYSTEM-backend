"""Notification delivery across channels.

In-app always happens — it is a database write and cannot fail for want of a
third-party account. Email, SMS and push are *attempted* through whichever
provider is configured, and a missing provider is recorded as skipped rather
than raised: a school without an SMS contract must still be able to publish a
notice.

Every attempt is logged to `notification_deliveries`, so "did the parent
actually get the fee reminder?" has an answer.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from enum import StrEnum
from typing import Any

import httpx
from bson import ObjectId

from app.core.config import settings
from app.core.context import TenantContext
from app.db.mongo import C, collection
from app.models.base import utcnow

log = logging.getLogger("scholarly.notify")


class Channel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    WHATSAPP = "whatsapp"


@dataclass(slots=True)
class Delivery:
    channel: Channel
    target: str
    status: str                # sent | skipped | failed
    detail: str = ""
    provider: str = ""


# ── Providers ─────────────────────────────────────────────────────────────
RESEND_API = "https://api.resend.com/emails"


async def _send_email(to: str, subject: str, body: str, html: str = "") -> Delivery:
    """Resend first, SMTP second, and a recorded skip when neither is set up."""
    if settings.resend_api_key and settings.mail_address:
        return await _send_email_resend(to, subject, body, html)
    if settings.smtp_host:
        return await _send_email_smtp(to, subject, body, html)
    return Delivery(Channel.EMAIL, to, "skipped", "No email provider configured")


async def _send_email_resend(to: str, subject: str, body: str, html: str) -> Delivery:
    payload: dict[str, Any] = {
        "from": settings.mail_sender,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    if html:
        payload["html"] = html
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                RESEND_API,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json=payload,
            )
        if response.status_code >= 400:
            # Resend says exactly what is wrong (unverified domain, bad address);
            # keeping the text is the difference between a fixable report and
            # "email did not work".
            detail = response.text[:200]
            log.warning("Resend rejected mail to %s: %s", to, detail)
            return Delivery(Channel.EMAIL, to, "failed", detail, "resend")
        return Delivery(Channel.EMAIL, to, "sent",
                        str(response.json().get("id", ""))[:60], "resend")
    except Exception as exc:
        log.warning("Resend call for %s failed: %s", to, exc)
        return Delivery(Channel.EMAIL, to, "failed", str(exc)[:200], "resend")


async def _send_email_smtp(to: str, subject: str, body: str, html: str = "") -> Delivery:
    message = EmailMessage()
    message["From"] = settings.mail_sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")

    def _blocking_send() -> None:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)

    try:
        # smtplib is blocking; keep it off the event loop.
        await asyncio.to_thread(_blocking_send)
        return Delivery(Channel.EMAIL, to, "sent", provider="smtp")
    except Exception as exc:  # pragma: no cover - depends on a live mail server
        log.warning("Email to %s failed: %s", to, exc)
        return Delivery(Channel.EMAIL, to, "failed", str(exc)[:200], "smtp")


async def _send_sms(to: str, body: str) -> Delivery:
    """Generic HTTP SMS gateway.

    Indian providers (MSG91, TextLocal, Fast2SMS, Gupshup) all accept a POST
    with an API key, so one templated request covers them. Set
    SMS_API_URL / SMS_API_KEY / SMS_SENDER_ID and the payload keys.
    """
    if not settings.sms_api_url or not settings.sms_api_key:
        return Delivery(Channel.SMS, to, "skipped", "No SMS gateway configured")

    payload = {
        settings.sms_field_to: to,
        settings.sms_field_message: body[:480],
    }
    if settings.sms_sender_id:
        payload[settings.sms_field_sender] = settings.sms_sender_id

    headers = {"Content-Type": "application/json"}
    if settings.sms_auth_header:
        headers[settings.sms_auth_header] = settings.sms_api_key
    else:
        payload["apikey"] = settings.sms_api_key

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(settings.sms_api_url, json=payload, headers=headers)
        if response.status_code >= 400:
            return Delivery(Channel.SMS, to, "failed", response.text[:200], "http")
        return Delivery(Channel.SMS, to, "sent", provider="http")
    except Exception as exc:  # pragma: no cover
        log.warning("SMS to %s failed: %s", to, exc)
        return Delivery(Channel.SMS, to, "failed", str(exc)[:200], "http")


async def _send_push(tokens: list[str], title: str, body: str, link: str = "") -> Delivery:
    """Firebase Cloud Messaging, via the legacy HTTP endpoint (one server key,
    no service-account JSON to ship)."""
    if not settings.fcm_server_key:
        return Delivery(Channel.PUSH, ",".join(tokens[:2]), "skipped", "No FCM key configured")
    if not tokens:
        return Delivery(Channel.PUSH, "", "skipped", "No device tokens registered")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://fcm.googleapis.com/fcm/send",
                headers={
                    "Authorization": f"key={settings.fcm_server_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "registration_ids": tokens[:500],
                    "notification": {"title": title, "body": body[:180]},
                    "data": {"link": link},
                },
            )
        if response.status_code >= 400:
            return Delivery(Channel.PUSH, f"{len(tokens)} devices", "failed",
                            response.text[:200], "fcm")
        return Delivery(Channel.PUSH, f"{len(tokens)} devices", "sent", provider="fcm")
    except Exception as exc:  # pragma: no cover
        log.warning("Push failed: %s", exc)
        return Delivery(Channel.PUSH, f"{len(tokens)} devices", "failed", str(exc)[:200], "fcm")


async def _send_whatsapp(to: str, body: str) -> Delivery:
    if not settings.whatsapp_api_url or not settings.whatsapp_token:
        return Delivery(Channel.WHATSAPP, to, "skipped", "No WhatsApp API configured")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                settings.whatsapp_api_url,
                headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": body[:1000]},
                },
            )
        if response.status_code >= 400:
            return Delivery(Channel.WHATSAPP, to, "failed", response.text[:200], "meta")
        return Delivery(Channel.WHATSAPP, to, "sent", provider="meta")
    except Exception as exc:  # pragma: no cover
        return Delivery(Channel.WHATSAPP, to, "failed", str(exc)[:200], "meta")


# ── Orchestration ─────────────────────────────────────────────────────────
def enabled_channels(tenant: TenantContext) -> set[Channel]:
    """An institution chooses which channels it uses, in settings."""
    configured = (tenant.settings or {}).get("notification_channels")
    if configured:
        return {Channel(c) for c in configured if c in set(Channel)}
    return {Channel.IN_APP, Channel.EMAIL, Channel.PUSH}


async def notify_users(
    tenant: TenantContext,
    user_ids: list[ObjectId],
    *,
    title: str,
    body: str = "",
    category: str = "general",
    type: str = "info",
    link: str = "",
    entity_type: str = "",
    entity_id: ObjectId | None = None,
    channels: set[Channel] | None = None,
    force_external: bool = False,
) -> dict[str, Any]:
    """Deliver to a set of users across every channel they have opted into."""
    if not user_ids:
        return {"recipients": 0, "deliveries": []}

    now = utcnow()
    # 1. In-app, always.
    await collection(C.NOTIFICATIONS).insert_many([
        {
            "tenant_id": tenant.id,
            "user_id": user_id,
            "title": title,
            "body": body,
            "type": type,
            "category": category,
            "link": link,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "read_at": None,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
        }
        for user_id in user_ids
    ])

    wanted = (channels or enabled_channels(tenant)) - {Channel.IN_APP}
    if not wanted:
        return {"recipients": len(user_ids), "deliveries": []}

    users = await collection(C.USERS).find(
        {"_id": {"$in": user_ids}, "tenant_id": tenant.id, "is_active": True},
        {"email": 1, "phone": 1, "full_name": 1, "preferences": 1, "device_tokens": 1},
    ).to_list(length=None)

    results: list[Delivery] = []
    push_tokens: list[str] = []

    for user in users:
        preferences = (user.get("preferences") or {}).get("notifications", {})
        for channel in wanted:
            # A user can mute a channel unless the message is marked important.
            if not force_external and preferences.get(str(channel)) is False:
                continue
            if channel is Channel.EMAIL and user.get("email"):
                results.append(await _send_email(user["email"], title, body or title))
            elif channel is Channel.SMS and user.get("phone"):
                results.append(await _send_sms(user["phone"], f"{title}\n{body}".strip()))
            elif channel is Channel.WHATSAPP and user.get("phone"):
                results.append(await _send_whatsapp(user["phone"], f"*{title}*\n{body}".strip()))
            elif channel is Channel.PUSH:
                push_tokens.extend(user.get("device_tokens") or [])

    if Channel.PUSH in wanted:
        results.append(await _send_push(push_tokens, title, body, link))

    await _record(tenant, title, category, results)
    return {
        "recipients": len(user_ids),
        "deliveries": [
            {"channel": str(d.channel), "status": d.status, "detail": d.detail}
            for d in results
        ],
    }


async def notify_audience(
    tenant: TenantContext,
    *,
    audience: dict[str, Any],
    title: str,
    body: str = "",
    category: str = "general",
    link: str = "",
    channels: set[Channel] | None = None,
) -> dict[str, Any]:
    """Resolve an `Audience` (roles / classes / sections) to users, then notify."""
    user_ids = await resolve_audience(tenant, audience)
    return await notify_users(
        tenant, user_ids, title=title, body=body, category=category,
        link=link, channels=channels,
    )


async def resolve_audience(tenant: TenantContext, audience: dict[str, Any]) -> list[ObjectId]:
    """Turn a notice's audience into the concrete list of user ids."""
    audience = audience or {}
    users = collection(C.USERS)
    base = {"tenant_id": tenant.id, "is_active": True, "is_deleted": {"$ne": True}}

    if audience.get("everyone"):
        return await users.distinct("_id", base)

    conditions: list[dict[str, Any]] = []

    if audience.get("user_ids"):
        conditions.append({"_id": {"$in": [ObjectId(u) for u in audience["user_ids"]
                                           if ObjectId.is_valid(u)]}})

    if audience.get("roles"):
        role_ids = await collection(C.ROLES).distinct(
            "_id", {"tenant_id": tenant.id, "key": {"$in": audience["roles"]}}
        )
        conditions.append({"role_ids": {"$in": role_ids}})

    section_ids = [ObjectId(s) for s in (audience.get("section_ids") or [])
                   if ObjectId.is_valid(s)]
    class_ids = [ObjectId(c) for c in (audience.get("class_ids") or [])
                 if ObjectId.is_valid(c)]

    if section_ids or class_ids:
        student_filter: dict[str, Any] = {"tenant_id": tenant.id, "status": "active",
                                          "is_deleted": {"$ne": True}}
        if section_ids:
            student_filter["current_section_id"] = {"$in": section_ids}
        elif class_ids:
            student_filter["current_class_id"] = {"$in": class_ids}

        students = await collection(C.STUDENTS).find(
            student_filter, {"_id": 1, "guardian_ids": 1}
        ).to_list(length=None)
        student_ids = [s["_id"] for s in students]
        guardian_ids = [g for s in students for g in (s.get("guardian_ids") or [])]

        conditions.append({"$or": [
            {"student_id": {"$in": student_ids}},
            {"guardian_id": {"$in": guardian_ids}},
        ]})

    if audience.get("department_ids"):
        department_ids = [ObjectId(d) for d in audience["department_ids"]
                          if ObjectId.is_valid(d)]
        staff_ids = await collection(C.STAFF).distinct(
            "_id", {"tenant_id": tenant.id, "department_id": {"$in": department_ids}}
        )
        conditions.append({"staff_id": {"$in": staff_ids}})

    if not conditions:
        return []
    return await users.distinct("_id", {**base, "$or": conditions})


async def _record(
    tenant: TenantContext, title: str, category: str, deliveries: list[Delivery]
) -> None:
    if not deliveries:
        return
    try:
        await collection(C.NOTIFICATION_DELIVERIES).insert_many([
            {
                "tenant_id": tenant.id,
                "title": title,
                "category": category,
                "channel": str(delivery.channel),
                "target": delivery.target,
                "status": delivery.status,
                "detail": delivery.detail,
                "provider": delivery.provider,
                "created_at": utcnow(),
                "is_deleted": False,
            }
            for delivery in deliveries
        ])
    except Exception as exc:  # never let logging break a send
        log.warning("Could not record deliveries: %s", exc)


def channel_status() -> list[dict[str, Any]]:
    """What is actually wired up — surfaced in settings so a school can see
    why its SMS is not going out."""
    return [
        {"channel": "in_app", "label": "In-app", "configured": True,
         "detail": "Always available"},
        {"channel": "email", "label": "Email", "configured": settings.email_enabled,
         "detail": (
             f"Resend, from {settings.mail_sender}"
             if settings.resend_api_key and settings.mail_address
             else settings.smtp_host or "Set RESEND_API_KEY and MAIL_ADDRESS to enable"
         )},
        {"channel": "sms", "label": "SMS", "configured": bool(settings.sms_api_url),
         "detail": settings.sms_api_url or "Set SMS_API_URL and SMS_API_KEY to enable"},
        {"channel": "push", "label": "Push", "configured": bool(settings.fcm_server_key),
         "detail": "Firebase" if settings.fcm_server_key else "Set FCM_SERVER_KEY to enable"},
        {"channel": "whatsapp", "label": "WhatsApp",
         "configured": bool(settings.whatsapp_api_url),
         "detail": settings.whatsapp_api_url or "Set WHATSAPP_API_URL and WHATSAPP_TOKEN"},
    ]
