"""The AI assistant endpoint."""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.core.config import settings
from app.core.deps import CurrentUser, TenantDep
from app.db.mongo import C, collection
from app.models.base import AppModel, serialize_doc
from app.modules.ai import service

router = APIRouter(prefix="/assistant", tags=["AI Assistant"])


class AskRequest(AppModel):
    question: str
    #: Prior turns, so a follow-up like "and for Class 8?" makes sense.
    history: list[dict] = []


@router.get("/status", summary="Whether the assistant is available")
async def status(auth: CurrentUser, tenant: TenantDep):
    return {
        "enabled": settings.ai_enabled,
        "model": settings.anthropic_model if settings.ai_enabled else None,
        "tools": [t["name"] for t in service.tools_for(auth, tenant)],
        "suggestions": service.suggestions(auth),
        "detail": (
            "Ask anything about your institution's data."
            if settings.ai_enabled
            else "Add ANTHROPIC_API_KEY to enable the assistant."
        ),
    }


@router.post("/ask", summary="Ask a question about your institution")
async def ask(payload: AskRequest, auth: CurrentUser, tenant: TenantDep, request: Request):
    """Answers are grounded in tool calls that run the same tenant-scoped,
    permission-checked queries the screens use. The assistant cannot read a
    record the asker could not open themselves, and it cannot write anything."""
    result = await service.ask(tenant, auth, payload.question, payload.history)
    await service.save_turn(
        tenant, auth, payload.question, result["answer"], result["tools_used"]
    )
    return result


@router.get("/history", summary="Your recent questions")
async def history(
    auth: CurrentUser, tenant: TenantDep, limit: Annotated[int, Query(le=100)] = 20
):
    docs = await collection(C.AI_CONVERSATIONS).find(
        {"tenant_id": tenant.id, "user_id": auth.user_id, "is_deleted": {"$ne": True}}
    ).sort([("created_at", -1)]).limit(limit).to_list(length=limit)
    return [serialize_doc(d) for d in docs]
