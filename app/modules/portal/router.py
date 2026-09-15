"""The student and parent portal.

These people hold read permissions for their own records and nothing else — a
student has ``attendance:read`` for their own register, not for the class's. The
administrative screens are built around a class picker and are useless to them,
so the portal gets its own small API rather than a permission exception carved
into the teacher's.

Everything here is scoped by who is asking, never by an id in the request.
"""

from typing import Any

from fastapi import APIRouter

from app.core.context import AuthContext, TenantContext
from app.core.deps import CurrentUser, TenantDep
from app.core.exceptions import Forbidden
from app.core.scoping import family_student_ids
from app.db.mongo import C, collection
from app.models.base import serialize_doc
from app.modules.people import service as people

router = APIRouter(prefix="/portal", tags=["Portal"])

#: CurrentUser is already an Annotated dependency; wrapping it in Depends()
#: again makes FastAPI try to resolve its *args and **kwargs as query fields.
Viewer = CurrentUser


def _as_dt(on: Any) -> Any:
    from datetime import UTC, datetime

    return datetime(on.year, on.month, on.day, tzinfo=UTC)


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


@router.get("/fees", summary="What this family owes, instalment by instalment")
async def fees(auth: Viewer, tenant: TenantDep):
    """The schedule and the ledger together — a portal home that made four
    requests would spend a free-tier cold start doing nothing useful."""
    from app.modules.fees import service as fees_service

    ids = await _my_students(auth, tenant)
    out = []
    for sid in ids:
        student = await collection(C.STUDENTS).find_one({"_id": sid, "tenant_id": tenant.id})
        out.append({
            "student": {
                "id": str(sid),
                "full_name": " ".join(filter(None, [
                    (student or {}).get("first_name"),
                    (student or {}).get("middle_name"),
                    (student or {}).get("last_name"),
                ])),
                "admission_number": (student or {}).get("admission_number", ""),
            },
            "plan": await fees_service.fee_plan(tenant, str(sid)),
            "ledger": await fees_service.student_ledger(tenant, str(sid)),
        })
    return {"students": out}


@router.get("/calendar", summary="The institution's year — holidays and events")
async def calendar(
    auth: CurrentUser,
    tenant: TenantDep,
    start: str | None = None,
    end: str | None = None,
):
    """Open to anyone signed in.

    A school's holiday calendar is published to parents on a notice board; it is
    not something to gate behind the permission that edits academic years.
    """
    from datetime import date, timedelta

    from app.modules.attendance.service import holidays_between

    today = date.today()
    first = date.fromisoformat(start) if start else today.replace(day=1)
    last = date.fromisoformat(end) if end else first + timedelta(days=370)

    class_id = None
    if auth.student_id or auth.guardian_id:
        ids = await family_student_ids(auth, tenant)
        if ids:
            student = await collection(C.STUDENTS).find_one(
                {"_id": ids[0], "tenant_id": tenant.id}, projection={"current_class_id": 1}
            )
            class_id = (student or {}).get("current_class_id")

    holidays = await holidays_between(tenant, first, last, class_id)
    events = await collection(C.EVENTS).find({
        "tenant_id": tenant.id, "is_deleted": {"$ne": True},
        "status": {"$nin": ["cancelled"]},
        "start_at": {"$gte": _as_dt(first), "$lte": _as_dt(last)},
    }).sort([("start_at", 1)]).to_list(length=300)

    return {
        "from": first.isoformat(),
        "to": last.isoformat(),
        "holidays": [
            {
                "id": str(h["_id"]),
                "name": h.get("name", ""),
                "type": h.get("type", "public"),
                "description": h.get("description", ""),
                "attendance_required": bool(h.get("attendance_required")),
                "start_date": h["_first"].isoformat(),
                "end_date": h["_last"].isoformat(),
            }
            for h in holidays
        ],
        "events": [
            {
                "id": str(e["_id"]),
                "title": e.get("title", ""),
                "category": e.get("category", "general"),
                "location": e.get("location", ""),
                "all_day": bool(e.get("all_day")),
                "start_at": e["start_at"].isoformat(),
                "end_at": e["end_at"].isoformat() if e.get("end_at") else None,
            }
            for e in events
        ],
    }


@router.get("/announcements", summary="Notices for this family")
async def announcements(auth: Viewer, tenant: TenantDep, limit: int = 10):
    await _my_students(auth, tenant)
    docs = await collection(C.ANNOUNCEMENTS).find({
        "tenant_id": tenant.id, "is_deleted": {"$ne": True}, "status": "published",
    }).sort([("published_at", -1)]).limit(limit).to_list(length=limit)
    return {"items": [serialize_doc(d) for d in docs]}
