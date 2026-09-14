"""Messaging, notifications and broadcast delivery."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request

from app.core.context import AuthContext
from app.core.deps import CurrentUser, TenantDep, require
from app.models.base import AppModel, Msg
from app.modules.communication import notify, service
from app.utils.audit import record

router = APIRouter(prefix="/messages", tags=["Messaging"])

Reader = Annotated[AuthContext, Depends(require("messages:read"))]
Sender = Annotated[AuthContext, Depends(require("messages:create"))]


class StartThreadRequest(AppModel):
    participant_ids: list[str]
    subject: str = ""
    body: str = ""
    context_type: str = ""
    context_id: str | None = None


class SendMessageRequest(AppModel):
    body: str
    attachments: list[dict] = []


@router.get("/contacts", summary="Who you are allowed to message")
async def contacts(auth: Reader, tenant: TenantDep, search: str = ""):
    return await service.allowed_contacts(tenant, auth, search=search)


@router.get("/threads", summary="Your conversations")
async def threads(
    auth: Reader,
    tenant: TenantDep,
    page: int = 1,
    page_size: Annotated[int, Query(le=100)] = 25,
    search: str = "",
    unread_only: bool = False,
):
    return await service.list_threads(
        tenant, auth, page=page, page_size=page_size, search=search, unread_only=unread_only
    )


@router.get("/unread-count", summary="Unread conversation count")
async def unread(auth: Reader, tenant: TenantDep):
    return {"unread": await service.unread_count(tenant, auth)}


@router.post("/threads", status_code=201, summary="Start a conversation")
async def start(payload: StartThreadRequest, auth: Sender, tenant: TenantDep, request: Request):
    result = await service.start_thread(
        tenant, auth,
        participant_ids=payload.participant_ids,
        subject=payload.subject,
        body=payload.body,
        context_type=payload.context_type,
        context_id=payload.context_id,
    )
    await record(auth, "messages.thread_started", entity_type="message_thread",
                 entity_id=result["id"], request=request)
    return result


@router.get("/threads/{thread_id}", summary="A conversation and its messages")
async def thread(thread_id: str, auth: Reader, tenant: TenantDep):
    return await service.get_thread(tenant, auth, thread_id)


@router.post("/threads/{thread_id}/messages", status_code=201, summary="Send a message")
async def send(
    thread_id: str, payload: SendMessageRequest, auth: Sender, tenant: TenantDep
):
    return await service.send_message(
        tenant, auth, thread_id, payload.body, payload.attachments
    )


@router.post("/threads/{thread_id}/read", response_model=Msg, summary="Mark as read")
async def mark_read(thread_id: str, auth: Reader, tenant: TenantDep):
    await service.mark_read(tenant, auth, thread_id)
    return Msg(detail="Marked read")


@router.post("/threads/{thread_id}/archive", response_model=Msg, summary="Archive")
async def archive(thread_id: str, auth: Reader, tenant: TenantDep):
    await service.archive_thread(tenant, auth, thread_id)
    return Msg(detail="Conversation archived")


# ── Broadcast ─────────────────────────────────────────────────────────────
broadcast_router = APIRouter(prefix="/broadcast", tags=["Communication"])

Broadcaster = Annotated[AuthContext, Depends(require("announcements:publish"))]


class BroadcastRequest(AppModel):
    title: str
    body: str = ""
    audience: dict = {}
    channels: list[str] = []
    category: str = "general"
    link: str = ""


@broadcast_router.get("/channels", summary="Which delivery channels are configured")
async def channels(auth: CurrentUser, tenant: TenantDep):
    return {
        "channels": notify.channel_status(),
        "enabled_for_institution": sorted(str(c) for c in notify.enabled_channels(tenant)),
    }


@broadcast_router.post("/preview", summary="How many people an audience reaches")
async def preview(
    auth: Broadcaster, tenant: TenantDep, audience: Annotated[dict, Body(embed=True)]
):
    user_ids = await notify.resolve_audience(tenant, audience)
    return {"recipients": len(user_ids)}


@broadcast_router.post("/send", summary="Send a notice across channels")
async def send_broadcast(
    payload: BroadcastRequest, auth: Broadcaster, tenant: TenantDep, request: Request
):
    """Delivers in-app always, plus whichever external channels are configured
    and selected. A channel with no provider is reported as skipped, not failed."""
    selected = (
        {notify.Channel(c) for c in payload.channels if c in set(notify.Channel)}
        if payload.channels else None
    )
    result = await notify.notify_audience(
        tenant,
        audience=payload.audience,
        title=payload.title,
        body=payload.body,
        category=payload.category,
        link=payload.link,
        channels=selected,
    )
    await record(auth, "broadcast.sent", entity_type="announcement",
                 entity_label=payload.title,
                 changes={"recipients": result["recipients"]}, request=request)
    return result


@broadcast_router.get("/deliveries", summary="Delivery log")
async def deliveries(
    auth: Annotated[AuthContext, Depends(require("announcements:read"))],
    tenant: TenantDep,
    page: int = 1,
    page_size: Annotated[int, Query(le=200)] = 50,
    channel: str = "",
    status: str = "",
):
    from app.db.mongo import C, collection
    from app.models.base import serialize_doc

    query: dict = {"tenant_id": tenant.id}
    if channel:
        query["channel"] = channel
    if status:
        query["status"] = status

    rows = collection(C.NOTIFICATION_DELIVERIES)
    total = await rows.count_documents(query)
    docs = await rows.find(query).sort([("created_at", -1)]).skip(
        (page - 1) * page_size
    ).limit(page_size).to_list(length=page_size)
    total_pages = max(1, -(-total // page_size))
    return {
        "items": [serialize_doc(d) for d in docs],
        "meta": {"page": page, "page_size": page_size, "total": total,
                 "total_pages": total_pages, "has_next": page < total_pages,
                 "has_prev": page > 1},
    }
