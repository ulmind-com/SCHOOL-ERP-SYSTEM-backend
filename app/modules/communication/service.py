"""Threaded messaging.

Who may talk to whom is not a free-for-all: a parent messages their child's
teachers and the office, a student messages their teachers, staff message each
other. `allowed_contacts` is the single place that decides, and every send is
re-checked against it — the picker is a convenience, not the control.
"""

from __future__ import annotations

from typing import Any

from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.core.exceptions import Forbidden, NotFound, ValidationError
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models.base import serialize_doc, utcnow

MAX_PARTICIPANTS = 50


async def _user_directory(tenant_id: ObjectId, user_ids: list[ObjectId]) -> dict[ObjectId, dict]:
    if not user_ids:
        return {}
    rows = await collection(C.USERS).find(
        {"_id": {"$in": user_ids}, "tenant_id": tenant_id},
        {"full_name": 1, "email": 1, "avatar_url": 1, "role_ids": 1, "student_id": 1,
         "staff_id": 1, "guardian_id": 1},
    ).to_list(length=None)
    return {row["_id"]: row for row in rows}


async def allowed_contacts(
    tenant: TenantContext, auth: AuthContext, *, search: str = ""
) -> list[dict[str, Any]]:
    """Everyone this user is permitted to start a conversation with."""
    users = collection(C.USERS)
    roles = {
        role["_id"]: role
        for role in await collection(C.ROLES).find(
            {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
        ).to_list(length=None)
    }
    staff_role_ids = [r for r, role in roles.items() if role.get("portal") in
                      {"admin", "finance", "teacher"}]

    base: dict[str, Any] = {
        "tenant_id": tenant.id,
        "is_active": True,
        "is_deleted": {"$ne": True},
        "_id": {"$ne": auth.user_id},
    }

    if auth.portal in {"student", "parent"}:
        # Families reach staff, never other families.
        base["role_ids"] = {"$in": staff_role_ids}
    elif not auth.can("messages:create"):
        raise Forbidden("You do not have permission to send messages")

    if search:
        base["$or"] = [
            {"full_name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
        ]

    rows = await users.find(
        base, {"full_name": 1, "email": 1, "avatar_url": 1, "role_ids": 1}
    ).sort([("full_name", 1)]).limit(200).to_list(length=200)

    return [
        {
            "id": str(row["_id"]),
            "full_name": row.get("full_name", ""),
            "email": row.get("email", ""),
            "avatar_url": row.get("avatar_url", ""),
            "roles": [
                roles.get(rid, {}).get("name", "")
                for rid in (row.get("role_ids") or [])
                if rid in roles
            ],
        }
        for row in rows
    ]


async def _assert_may_message(
    tenant: TenantContext, auth: AuthContext, recipient_ids: list[ObjectId]
) -> None:
    permitted = {ObjectId(c["id"]) for c in await allowed_contacts(tenant, auth)}
    blocked = [r for r in recipient_ids if r not in permitted and r != auth.user_id]
    if blocked:
        raise Forbidden("You cannot message one or more of those people")


async def list_threads(
    tenant: TenantContext, auth: AuthContext, *, page: int = 1, page_size: int = 25,
    search: str = "", unread_only: bool = False,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "tenant_id": tenant.id,
        "participant_ids": auth.user_id,
        "is_deleted": {"$ne": True},
    }
    if unread_only:
        query["unread_by"] = auth.user_id
    if search:
        query["$or"] = [
            {"subject": {"$regex": search, "$options": "i"}},
            {"participant_names": {"$regex": search, "$options": "i"}},
        ]

    threads = collection(C.THREADS)
    total = await threads.count_documents(query)
    docs = await threads.find(query).sort([("last_message_at", -1)]).skip(
        (page - 1) * page_size
    ).limit(page_size).to_list(length=page_size)

    directory = await _user_directory(
        tenant.id, [p for d in docs for p in (d.get("participant_ids") or [])]
    )

    items = []
    for doc in docs:
        row = serialize_doc(doc) or {}
        others = [p for p in (doc.get("participant_ids") or []) if p != auth.user_id]
        row["participants"] = [
            {
                "id": str(p),
                "full_name": directory.get(p, {}).get("full_name", "Removed user"),
                "avatar_url": directory.get(p, {}).get("avatar_url", ""),
            }
            for p in others
        ]
        row["title"] = doc.get("subject") or ", ".join(
            p["full_name"] for p in row["participants"]
        ) or "Conversation"
        row["unread"] = auth.user_id in (doc.get("unread_by") or [])
        items.append(row)

    total_pages = max(1, -(-total // page_size))
    return {
        "items": items,
        "meta": {"page": page, "page_size": page_size, "total": total,
                 "total_pages": total_pages, "has_next": page < total_pages,
                 "has_prev": page > 1},
    }


async def get_thread(
    tenant: TenantContext, auth: AuthContext, thread_id: str, *, limit: int = 100
) -> dict[str, Any]:
    thread = await collection(C.THREADS).find_one(
        {"_id": ObjectId(thread_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if thread is None:
        raise NotFound("Conversation not found")
    if auth.user_id not in (thread.get("participant_ids") or []):
        raise Forbidden("You are not part of this conversation")

    messages = await collection(C.MESSAGES).find(
        {"tenant_id": tenant.id, "thread_id": thread["_id"], "is_deleted": {"$ne": True}}
    ).sort([("created_at", 1)]).limit(limit).to_list(length=limit)

    directory = await _user_directory(tenant.id, thread.get("participant_ids") or [])

    await mark_read(tenant, auth, thread_id)

    out = serialize_doc(thread) or {}
    out["participants"] = [
        {
            "id": str(p),
            "full_name": directory.get(p, {}).get("full_name", "Removed user"),
            "avatar_url": directory.get(p, {}).get("avatar_url", ""),
            "is_me": p == auth.user_id,
        }
        for p in (thread.get("participant_ids") or [])
    ]
    out["title"] = thread.get("subject") or ", ".join(
        p["full_name"] for p in out["participants"] if not p["is_me"]
    ) or "Conversation"
    out["messages"] = [
        {
            **(serialize_doc(message) or {}),
            "is_mine": message.get("sender_id") == auth.user_id,
        }
        for message in messages
    ]
    return out


async def start_thread(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    participant_ids: list[str],
    subject: str = "",
    body: str = "",
    context_type: str = "",
    context_id: str | None = None,
) -> dict[str, Any]:
    recipients = [ObjectId(p) for p in participant_ids if ObjectId.is_valid(p)]
    recipients = [r for r in recipients if r != auth.user_id]
    if not recipients:
        raise ValidationError("Choose at least one person to message")
    if len(recipients) > MAX_PARTICIPANTS:
        raise ValidationError(f"A conversation can hold at most {MAX_PARTICIPANTS} people")

    await _assert_may_message(tenant, auth, recipients)

    everyone = sorted({auth.user_id, *recipients})
    threads = Repository(C.THREADS, tenant.id, actor_id=auth.user_id)

    # A one-to-one conversation without a subject reuses the existing thread —
    # nobody wants five separate chats with the same teacher.
    existing = None
    if len(everyone) == 2 and not subject:
        existing = await threads.find_one(
            {"participant_ids": everyone, "type": "direct", "subject": ""}
        )

    if existing:
        thread = existing
    else:
        directory = await _user_directory(tenant.id, everyone)
        thread = await threads.create({
            "subject": subject.strip(),
            "participant_ids": everyone,
            "participant_names": [
                directory.get(p, {}).get("full_name", "") for p in everyone
            ],
            "type": "direct" if len(everyone) == 2 else "group",
            "context_type": context_type,
            "context_id": ObjectId(context_id) if context_id and ObjectId.is_valid(context_id) else None,
            "last_message": "",
            "last_message_at": utcnow(),
            "unread_by": [],
            "is_archived": False,
        })

    if body.strip():
        await send_message(tenant, auth, str(thread["_id"]), body)

    return {"id": str(thread["_id"]), "detail": "Conversation started"}


async def send_message(
    tenant: TenantContext, auth: AuthContext, thread_id: str, body: str,
    attachments: list[dict] | None = None,
) -> dict[str, Any]:
    body = (body or "").strip()
    if not body and not attachments:
        raise ValidationError("Write something to send")
    if len(body) > 5000:
        raise ValidationError("Messages are limited to 5,000 characters")

    thread = await collection(C.THREADS).find_one(
        {"_id": ObjectId(thread_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if thread is None:
        raise NotFound("Conversation not found")
    participants = thread.get("participant_ids") or []
    if auth.user_id not in participants:
        raise Forbidden("You are not part of this conversation")

    message = await Repository(C.MESSAGES, tenant.id, actor_id=auth.user_id).create({
        "thread_id": thread["_id"],
        "sender_id": auth.user_id,
        "sender_name": auth.full_name,
        "body": body,
        "attachments": attachments or [],
        "read_by": [auth.user_id],
    })

    others = [p for p in participants if p != auth.user_id]
    await collection(C.THREADS).update_one(
        {"_id": thread["_id"]},
        {"$set": {
            "last_message": body[:160],
            "last_message_at": utcnow(),
            "unread_by": others,
            "updated_at": utcnow(),
        }},
    )

    # A message the recipient hasn't opened yet should still reach them.
    from app.modules.communication.notify import notify_users

    await notify_users(
        tenant, others,
        title=f"New message from {auth.full_name}",
        body=body[:140],
        category="message",
        link=f"/communication/messages?thread={thread['_id']}",
        entity_type="message_thread",
        entity_id=thread["_id"],
    )

    return {**(serialize_doc(message) or {}), "is_mine": True}


async def mark_read(tenant: TenantContext, auth: AuthContext, thread_id: str) -> None:
    await collection(C.THREADS).update_one(
        {"_id": ObjectId(thread_id), "tenant_id": tenant.id},
        {"$pull": {"unread_by": auth.user_id}},
    )
    await collection(C.MESSAGES).update_many(
        {"tenant_id": tenant.id, "thread_id": ObjectId(thread_id),
         "read_by": {"$ne": auth.user_id}},
        {"$addToSet": {"read_by": auth.user_id}},
    )


async def unread_count(tenant: TenantContext, auth: AuthContext) -> int:
    return await collection(C.THREADS).count_documents(
        {"tenant_id": tenant.id, "unread_by": auth.user_id, "is_deleted": {"$ne": True}}
    )


async def archive_thread(tenant: TenantContext, auth: AuthContext, thread_id: str) -> None:
    thread = await collection(C.THREADS).find_one(
        {"_id": ObjectId(thread_id), "tenant_id": tenant.id}
    )
    if thread is None:
        raise NotFound("Conversation not found")
    if auth.user_id not in (thread.get("participant_ids") or []):
        raise Forbidden("You are not part of this conversation")
    await collection(C.THREADS).update_one(
        {"_id": thread["_id"]}, {"$set": {"is_archived": True, "updated_at": utcnow()}}
    )
