"""Append-only activity trail. Every institution can see who changed what."""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from fastapi import Request

from app.core.context import AuthContext
from app.db.mongo import C, collection
from app.models.base import utcnow

log = logging.getLogger("scholarly.audit")


async def record(
    auth: AuthContext | None,
    action: str,
    *,
    entity_type: str = "",
    entity_id: ObjectId | str | None = None,
    entity_label: str = "",
    changes: dict[str, Any] | None = None,
    request: Request | None = None,
    tenant_id: ObjectId | None = None,
) -> None:
    tid = tenant_id or (auth.tenant_id if auth else None)
    target = C.PLATFORM_AUDIT if tid is None else C.AUDIT
    doc: dict[str, Any] = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": ObjectId(entity_id) if isinstance(entity_id, str) and ObjectId.is_valid(entity_id) else entity_id,
        "entity_label": entity_label,
        "changes": changes or {},
        "actor_id": auth.user_id if auth else None,
        "actor_name": auth.full_name if auth else "system",
        # An impersonated session writes under the borrowed account, so without
        # this the trail would say the student changed their own marks.
        "on_behalf_of_id": auth.impersonated_by_id if auth else None,
        "on_behalf_of_name": auth.impersonated_by_name if auth else "",
        "impersonated": bool(auth.impersonating) if auth else False,
        "created_at": utcnow(),
        "is_deleted": False,
    }
    if tid is not None:
        doc["tenant_id"] = tid
    if request is not None:
        doc["ip"] = client_ip(request)
        doc["user_agent"] = request.headers.get("user-agent", "")[:300]
    try:
        await collection(target).insert_one(doc)
    except Exception as exc:  # never let auditing break the request
        log.warning("audit write failed for %s: %s", action, exc)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def diff(before: dict[str, Any], after: dict[str, Any], *, skip: set[str] | None = None) -> dict:
    """Field-level before/after, for the audit trail's ``changes``."""
    skip = (skip or set()) | {"updated_at", "updated_by", "created_at", "created_by",
                              "password_hash", "_id", "tenant_id"}
    out: dict[str, dict[str, Any]] = {}
    for key, new in after.items():
        if key in skip:
            continue
        old = before.get(key)
        if old != new:
            out[key] = {"from": _plain(old), "to": _plain(new)}
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
