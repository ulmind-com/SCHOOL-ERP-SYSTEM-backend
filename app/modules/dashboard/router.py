"""Dashboards — a different answer per role from one endpoint.

An admin needs collection rates and headcount; a teacher needs today's classes
and whose homework is outstanding; a parent needs their child's attendance and
what is due. Same URL, different payload, decided by the caller's portal.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from bson import ObjectId
from fastapi import APIRouter, Depends

from app.core.context import AuthContext, TenantContext
from app.core.deps import TenantDep, require
from app.db.mongo import C, collection
from app.models.base import serialize_doc

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

Viewer = Annotated[AuthContext, Depends(require("dashboard:read"))]

PRESENT_LIKE = ["present", "late", "half_day"]


def _today() -> datetime:
    today = date.today()
    return datetime(today.year, today.month, today.day, tzinfo=UTC)


async def _holiday_panel(
    tenant: TenantContext, class_id: ObjectId | None = None, ahead: int = 4
) -> dict[str, Any]:
    """Today's holiday if there is one, and the next few coming.

    On every dashboard because it answers the same question for everyone, just
    for different reasons: a parent plans around it, a teacher stops wondering
    why no register is due, an administrator sees the term taking shape.
    """
    from app.modules.attendance.service import holiday_on, holidays_between

    on = date.today()
    today = await holiday_on(tenant, on, class_id)
    upcoming = await holidays_between(tenant, on, on + timedelta(days=120), class_id)

    def shape(row: dict) -> dict[str, Any]:
        first, last = row["_first"], row["_last"]
        return {
            "id": str(row["_id"]),
            "name": row.get("name", ""),
            "type": row.get("type", "public"),
            "start_date": first.isoformat(),
            "end_date": last.isoformat(),
            "days": (last - first).days + 1,
            "starts_in": (first - on).days,
            "attendance_required": bool(row.get("attendance_required")),
        }

    return {
        "today": {
            "name": today.get("name", ""),
            "type": today.get("type", "public"),
            "attendance_required": bool(today.get("attendance_required")),
            "end_date": today["_last"].isoformat(),
        } if today else None,
        # Skip the one already running — it is reported above, and repeating it
        # as "upcoming" reads as a second holiday.
        "upcoming": [
            shape(row) for row in upcoming
            if row["_first"] > on
        ][:ahead],
    }


async def _holidays_needing_attention(tenant: TenantContext) -> list[dict[str, Any]]:
    """Closed holidays that still have attendance recorded inside them.

    Only looks at recent ones: a break three years ago with stray registers is
    history, not a task.
    """
    from app.modules.attendance.service import holiday_conflicts

    since = _today() - timedelta(days=120)
    rows = await collection(C.HOLIDAYS).find({
        "tenant_id": tenant.id, "is_deleted": {"$ne": True}, "is_active": True,
        "attendance_required": {"$ne": True}, "start_date": {"$gte": since},
    }).sort([("start_date", -1)]).limit(20).to_list(length=20)

    out = []
    for row in rows:
        report = await holiday_conflicts(tenant, str(row["_id"]))
        if report["records"]:
            out.append({
                "id": str(row["_id"]),
                "name": row.get("name", ""),
                "records": report["records"],
                "dates": report["dates"],
            })
    return out


@router.get("", summary="Dashboard for the signed-in user")
async def dashboard(auth: Viewer, tenant: TenantDep):
    if auth.portal == "student" and auth.student_id:
        return await _student_dashboard(tenant, auth)
    if auth.portal == "parent" and auth.guardian_id:
        return await _parent_dashboard(tenant, auth)
    if auth.portal == "teacher" and auth.staff_id:
        return await _teacher_dashboard(tenant, auth)
    return await _admin_dashboard(tenant, auth)


# ── Admin / finance ───────────────────────────────────────────────────────
async def _admin_dashboard(tenant: TenantContext, auth: AuthContext) -> dict[str, Any]:
    live = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    today = _today()
    month_start = datetime(date.today().year, date.today().month, 1, tzinfo=UTC)

    students = await collection(C.STUDENTS).count_documents({**live, "status": "active"})
    staff = await collection(C.STAFF).count_documents({**live, "status": "active"})
    classes = await collection(C.CLASSES).count_documents({**live, "is_active": True})

    # Today's attendance
    att = await collection(C.ATTENDANCE).aggregate([
        {"$match": {**live, "date": today, "session_key": "day"}},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]).to_list(length=None)
    counts = {r["_id"]: r["n"] for r in att}
    marked = sum(counts.values())
    present = sum(counts.get(s, 0) for s in PRESENT_LIKE)

    # Fees
    billed = await collection(C.FEE_INVOICES).aggregate([
        {"$match": {**live, "status": {"$nin": ["draft", "cancelled"]}}},
        {"$group": {"_id": None, "total": {"$sum": "$total"},
                    "paid": {"$sum": "$paid_amount"}}},
    ]).to_list(length=1)
    fee = billed[0] if billed else {"total": 0, "paid": 0}
    collected_this_month = await collection(C.PAYMENTS).aggregate([
        {"$match": {**live, "status": "success", "paid_at": {"$gte": month_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ]).to_list(length=1)

    # Trends — attendance for the last 14 days
    trend = await collection(C.ATTENDANCE).aggregate([
        {"$match": {**live, "session_key": "day",
                    "date": {"$gte": today - timedelta(days=14)}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "total": {"$sum": 1},
            "present": {"$sum": {"$cond": [{"$in": ["$status", PRESENT_LIKE]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]).to_list(length=None)

    gender = await collection(C.STUDENTS).aggregate([
        {"$match": {**live, "status": "active"}},
        {"$group": {"_id": "$gender", "n": {"$sum": 1}}},
    ]).to_list(length=None)

    by_class = await collection(C.STUDENTS).aggregate([
        {"$match": {**live, "status": "active"}},
        {"$group": {"_id": "$current_class_id", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 12},
    ]).to_list(length=None)
    class_names = {
        c["_id"]: c.get("name", "")
        for c in await collection(C.CLASSES).find(
            {"_id": {"$in": [r["_id"] for r in by_class if r["_id"]]}}
        ).to_list(length=None)
    }

    announcements = await collection(C.ANNOUNCEMENTS).find(
        {**live, "status": "published"}
    ).sort([("published_at", -1)]).limit(5).to_list(length=5)
    events = await collection(C.EVENTS).find(
        {**live, "start_at": {"$gte": today}}
    ).sort([("start_at", 1)]).limit(5).to_list(length=5)

    total_billed = round(float(fee.get("total", 0)), 2)
    total_paid = round(float(fee.get("paid", 0)), 2)

    holidays = await _holiday_panel(tenant)
    # A closed holiday with registers inside it is a job for the office, so it
    # belongs on their screen rather than waiting to be noticed.
    pending_holiday_fixes = await _holidays_needing_attention(tenant)

    return {
        "portal": "admin",
        "institution": {"name": tenant.name, "type": tenant.institution_type,
                        "deployment": tenant.deployment},
        "holidays": {**holidays, "needs_attention": pending_holiday_fixes},
        "stats": {
            "students": students,
            "staff": staff,
            "classes": classes,
            "attendance_today": {
                "marked": marked,
                "present": present,
                "absent": counts.get("absent", 0),
                "percentage": round(present / marked * 100, 2) if marked else 0.0,
                # Nought per cent on a day the school is shut is not a number
                # anyone should read as a number.
                "holiday": (holidays["today"] or {}).get("name") or "",
                "counts": bool(
                    holidays["today"] is None
                    or (holidays["today"] or {}).get("attendance_required")
                ),
            },
            "fees": {
                "billed": total_billed,
                "collected": total_paid,
                "outstanding": round(total_billed - total_paid, 2),
                "collection_rate": (
                    round(total_paid / total_billed * 100, 2) if total_billed else 0.0
                ),
                "this_month": round(
                    float(collected_this_month[0]["total"]) if collected_this_month else 0, 2
                ),
                "receipts_this_month": (
                    collected_this_month[0]["count"] if collected_this_month else 0
                ),
            },
        },
        "attendance_trend": [
            {"date": r["_id"], "total": r["total"], "present": r["present"],
             "percentage": round(r["present"] / r["total"] * 100, 2) if r["total"] else 0.0}
            for r in trend
        ],
        "students_by_gender": [{"gender": r["_id"] or "undisclosed", "count": r["n"]}
                               for r in gender],
        "students_by_class": [
            {"class": class_names.get(r["_id"], "Unassigned"), "count": r["n"]}
            for r in by_class
        ],
        "announcements": [serialize_doc(a) for a in announcements],
        "upcoming_events": [serialize_doc(e) for e in events],
        "limits": (
            None if tenant.is_dedicated
            else {
                "students": {"used": students, "ceiling": tenant.limits.max_students},
                "staff": {"used": staff, "ceiling": tenant.limits.max_staff},
            }
        ),
    }


# ── Teacher ───────────────────────────────────────────────────────────────
async def _teacher_dashboard(tenant: TenantContext, auth: AuthContext) -> dict[str, Any]:
    live = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    today = _today()
    weekday = date.today().isoweekday()

    slots = await collection(C.TIMETABLE_SLOTS).find(
        {**live, "staff_id": auth.staff_id, "day_of_week": weekday}
    ).sort([("start_time", 1)]).to_list(length=20)

    section_ids = await collection(C.SECTIONS).distinct(
        "_id", {**live, "class_teacher_id": auth.staff_id}
    )
    assigned_sections = await collection(C.SUBJECT_ASSIGNMENTS).distinct(
        "section_id", {**live, "staff_id": auth.staff_id}
    )
    all_sections = list({*section_ids, *assigned_sections})

    students = await collection(C.STUDENTS).count_documents(
        {**live, "current_section_id": {"$in": all_sections}, "status": "active"}
    )
    pending_registers = 0
    for sid in all_sections:
        taken = await collection(C.ATTENDANCE_SESSIONS).find_one(
            {**live, "section_id": sid, "date": today, "session_key": "day"}
        )
        if taken is None:
            pending_registers += 1

    assignments = await collection(C.ASSIGNMENTS).find(
        {**live, "assigned_by": auth.user_id, "status": "published"}
    ).sort([("due_date", 1)]).limit(6).to_list(length=6)
    ungraded = await collection(C.SUBMISSIONS).count_documents(
        {**live, "status": "submitted",
         "assignment_id": {"$in": [a["_id"] for a in assignments]}}
    )

    return {
        "portal": "teacher",
        "holidays": await _holiday_panel(tenant),
        "stats": {
            "my_sections": len(all_sections),
            "my_students": students,
            "classes_today": len(slots),
            "registers_pending": pending_registers,
            "assignments_open": len(assignments),
            "submissions_to_grade": ungraded,
        },
        "today_schedule": [serialize_doc(s) for s in slots],
        "assignments": [serialize_doc(a) for a in assignments],
        "sections": [str(s) for s in all_sections],
    }


# ── Student ───────────────────────────────────────────────────────────────
async def _student_dashboard(tenant: TenantContext, auth: AuthContext) -> dict[str, Any]:
    from app.modules.people.service import attendance_summary, fee_summary

    live = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    student = await collection(C.STUDENTS).find_one({"_id": auth.student_id, **live}) or {}
    weekday = date.today().isoweekday()

    schedule = await collection(C.TIMETABLE_SLOTS).find(
        {**live, "section_id": student.get("current_section_id"), "day_of_week": weekday}
    ).sort([("start_time", 1)]).to_list(length=20)

    assignments = await collection(C.ASSIGNMENTS).find(
        {**live, "section_ids": student.get("current_section_id"), "status": "published",
         "due_date": {"$gte": _today()}}
    ).sort([("due_date", 1)]).limit(6).to_list(length=6)

    cards = await collection(C.REPORT_CARDS).find(
        {**live, "student_id": auth.student_id, "published_at": {"$ne": None}}
    ).sort([("published_at", -1)]).limit(3).to_list(length=3)

    return {
        "portal": "student",
        "holidays": await _holiday_panel(tenant, student.get("current_class_id")),
        "student": {
            "name": " ".join(filter(None, [student.get("first_name"),
                                           student.get("last_name")])),
            "admission_number": student.get("admission_number"),
            "roll_number": student.get("roll_number"),
            "photo": (student.get("photo") or {}).get("url", ""),
        },
        "attendance": await attendance_summary(tenant.id, auth.student_id),  # type: ignore[arg-type]
        "fees": await fee_summary(tenant.id, auth.student_id),  # type: ignore[arg-type]
        "today_schedule": [serialize_doc(s) for s in schedule],
        "assignments_due": [serialize_doc(a) for a in assignments],
        "report_cards": [serialize_doc(c) for c in cards],
    }


# ── Parent ────────────────────────────────────────────────────────────────
async def _parent_dashboard(tenant: TenantContext, auth: AuthContext) -> dict[str, Any]:
    from app.modules.people.service import attendance_summary, fee_summary

    live = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    guardian = await collection(C.GUARDIANS).find_one({"_id": auth.guardian_id, **live}) or {}
    child_ids: list[ObjectId] = guardian.get("student_ids") or []

    children = []
    for child_id in child_ids:
        student = await collection(C.STUDENTS).find_one({"_id": child_id, **live})
        if student is None:
            continue
        school_class = await collection(C.CLASSES).find_one(
            {"_id": student.get("current_class_id")}
        )
        section = await collection(C.SECTIONS).find_one(
            {"_id": student.get("current_section_id")}
        )
        children.append({
            "id": str(child_id),
            "name": " ".join(filter(None, [student.get("first_name"),
                                           student.get("last_name")])),
            "admission_number": student.get("admission_number"),
            "photo": (student.get("photo") or {}).get("url", ""),
            "class": (school_class or {}).get("name", ""),
            "section": (section or {}).get("name", ""),
            "attendance": await attendance_summary(tenant.id, child_id),
            "fees": await fee_summary(tenant.id, child_id),
        })

    announcements = await collection(C.ANNOUNCEMENTS).find(
        {**live, "status": "published"}
    ).sort([("published_at", -1)]).limit(5).to_list(length=5)

    return {
        "portal": "parent",
        "holidays": await _holiday_panel(tenant),
        "guardian": {"name": guardian.get("full_name", "")},
        "children": children,
        "total_outstanding": round(
            sum(c["fees"]["outstanding"] for c in children), 2
        ),
        "announcements": [serialize_doc(a) for a in announcements],
    }
