"""The student and parent portal.

These people hold read permissions for their own records and nothing else — a
student has ``attendance:read`` for their own register, not for the class's. The
administrative screens are built around a class picker and are useless to them,
so the portal gets its own small API rather than a permission exception carved
into the teacher's.

Everything here is scoped by who is asking, never by an id in the request.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.context import AuthContext, TenantContext
from app.core.deps import CurrentUser, TenantDep
from app.core.exceptions import Forbidden
from app.core.scoping import family_student_ids
from app.db.mongo import C, collection
from app.models.base import serialize_doc
from app.modules.people import service as people

router = APIRouter(prefix="/portal", tags=["Portal"])

Viewer = Annotated[AuthContext, Depends(CurrentUser)]


async def _my_students(auth: AuthContext, tenant: TenantContext) -> list[Any]:
    allowed = await family_student_ids(auth, tenant)
    if allowed is None:
        # Staff have the administrative screens; sending them here would only
        # produce a confusing half-view of the whole school.
        raise Forbidden("This view is for students and parents")
    if not allowed:
        return []
    return allowed


@router.get("/me", summary="Everything the signed-in family's portal needs")
async def me(auth: Viewer, tenant: TenantDep):
    """One call per student: profile, attendance day by day, marks, fees, files.

    A portal home that made eight requests would spend its first two seconds on
    a free-tier cold start doing nothing useful.
    """
    ids = await _my_students(auth, tenant)
    return {
        "students": [await people.student_profile(tenant, str(sid)) for sid in ids],
        "viewer": {
            "portal": auth.portal,
            "full_name": auth.full_name,
            "is_parent": bool(auth.guardian_id),
        },
    }


@router.get("/announcements", summary="Notices for this family")
async def announcements(auth: Viewer, tenant: TenantDep, limit: int = 10):
    await _my_students(auth, tenant)
    docs = await collection(C.ANNOUNCEMENTS).find({
        "tenant_id": tenant.id, "is_deleted": {"$ne": True}, "status": "published",
    }).sort([("published_at", -1)]).limit(limit).to_list(length=limit)
    return {"items": [serialize_doc(d) for d in docs]}
