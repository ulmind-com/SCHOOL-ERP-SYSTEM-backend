"""Student, guardian and staff logic that is more than a database write."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.core.exceptions import NotFound, ValidationError
from app.db.mongo import C, collection
from app.db.repository import Repository, next_sequence
from app.models.base import serialize_doc, utcnow
from app.models.people import age_on


# ── Identifier generation ─────────────────────────────────────────────────
async def generate_admission_number(tenant: TenantContext) -> str:
    """``<prefix><year><0001>`` — configurable per institution, sequential and
    collision-free because the counter increments atomically."""
    conf = tenant.settings.get("admission_number", {}) if tenant.settings else {}
    prefix = conf.get("prefix", "ADM")
    include_year = conf.get("include_year", True)
    width = int(conf.get("width", 4))
    year = datetime.now(UTC).year
    seq = await next_sequence(tenant.id, f"admission:{year}" if include_year else "admission")
    parts = [prefix]
    if include_year:
        parts.append(str(year))
    parts.append(str(seq).zfill(width))
    return "".join(parts)


async def generate_employee_id(tenant: TenantContext) -> str:
    conf = tenant.settings.get("employee_id", {}) if tenant.settings else {}
    prefix = conf.get("prefix", "EMP")
    width = int(conf.get("width", 4))
    seq = await next_sequence(tenant.id, "employee")
    return f"{prefix}{str(seq).zfill(width)}"


async def next_roll_number(tenant_id: ObjectId, section_id: ObjectId | None) -> str:
    if section_id is None:
        return ""
    last = await collection(C.STUDENTS).find_one(
        {"tenant_id": tenant_id, "current_section_id": section_id, "is_deleted": {"$ne": True}},
        sort=[("roll_number", -1)],
    )
    try:
        return str(int(last.get("roll_number", 0)) + 1) if last else "1"
    except (ValueError, TypeError):
        return ""


# ── Section strength ──────────────────────────────────────────────────────
async def recount_section(tenant_id: ObjectId, section_id: ObjectId | None) -> None:
    if section_id is None:
        return
    count = await collection(C.STUDENTS).count_documents(
        {"tenant_id": tenant_id, "current_section_id": section_id,
         "status": "active", "is_deleted": {"$ne": True}}
    )
    await collection(C.SECTIONS).update_one(
        {"_id": section_id, "tenant_id": tenant_id},
        {"$set": {"current_strength": count, "updated_at": utcnow()}},
    )


async def check_section_capacity(tenant_id: ObjectId, section_id: ObjectId | None) -> None:
    if section_id is None:
        return
    section = await collection(C.SECTIONS).find_one({"_id": section_id, "tenant_id": tenant_id})
    if section is None:
        raise NotFound("Section not found")
    capacity = int(section.get("capacity") or 0)
    if not capacity:
        return
    strength = await collection(C.STUDENTS).count_documents(
        {"tenant_id": tenant_id, "current_section_id": section_id,
         "status": "active", "is_deleted": {"$ne": True}}
    )
    if strength >= capacity:
        raise ValidationError(
            f"Section {section.get('name', '')} is full ({strength}/{capacity}). "
            "Raise its capacity or choose another section."
        )


# ── Profile assembly ──────────────────────────────────────────────────────
async def student_profile(tenant: TenantContext, student_id: str) -> dict[str, Any]:
    """Everything a student's page needs, in one request rather than eight."""
    _id = ObjectId(student_id)
    students = collection(C.STUDENTS)
    student = await students.find_one(
        {"_id": _id, "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if student is None:
        raise NotFound("Student not found")

    out = serialize_doc(student) or {}
    out["full_name"] = " ".join(
        p for p in (student.get("first_name"), student.get("middle_name"),
                    student.get("last_name")) if p
    )
    out["age"] = age_on(student.get("date_of_birth"))

    # Placement
    out["school_class"] = serialize_doc(
        await collection(C.CLASSES).find_one({"_id": student.get("current_class_id")})
    )
    out["section"] = serialize_doc(
        await collection(C.SECTIONS).find_one({"_id": student.get("current_section_id")})
    )

    # Guardians
    guardian_ids = student.get("guardian_ids") or []
    out["guardians"] = [
        serialize_doc(g)
        for g in await collection(C.GUARDIANS).find(
            {"_id": {"$in": guardian_ids}, "tenant_id": tenant.id}
        ).to_list(length=None)
    ]

    # Attendance this academic year
    out["attendance"] = await attendance_summary(tenant.id, _id)

    # Fees
    out["fees"] = await fee_summary(tenant.id, _id)

    # Recent results
    cards = await collection(C.REPORT_CARDS).find(
        {"tenant_id": tenant.id, "student_id": _id, "is_deleted": {"$ne": True}}
    ).sort([("created_at", -1)]).limit(5).to_list(length=5)
    out["report_cards"] = [serialize_doc(c) for c in cards]

    # Login
    user = await collection(C.USERS).find_one(
        {"tenant_id": tenant.id, "student_id": _id, "is_deleted": {"$ne": True}}
    )
    out["login"] = (
        {"id": str(user["_id"]), "email": user.get("email"), "status": user.get("status"),
         "last_login_at": user.get("last_login_at")}
        if user else None
    )
    return out


async def attendance_summary(tenant_id: ObjectId, student_id: ObjectId) -> dict[str, Any]:
    rows = await collection(C.ATTENDANCE).aggregate(
        [
            {"$match": {"tenant_id": tenant_id, "student_id": student_id,
                        "session_key": "day", "is_deleted": {"$ne": True}}},
            {"$group": {"_id": "$status", "n": {"$sum": 1}}},
        ]
    ).to_list(length=None)
    counts = {r["_id"]: r["n"] for r in rows}
    total = sum(counts.values())
    present = counts.get("present", 0) + counts.get("late", 0) + counts.get("half_day", 0) * 0.5
    return {
        "total_days": total,
        "present": counts.get("present", 0),
        "absent": counts.get("absent", 0),
        "late": counts.get("late", 0),
        "leave": counts.get("leave", 0),
        "percentage": round(present / total * 100, 2) if total else 0.0,
    }


async def fee_summary(tenant_id: ObjectId, student_id: ObjectId) -> dict[str, Any]:
    rows = await collection(C.FEE_INVOICES).aggregate(
        [
            {"$match": {"tenant_id": tenant_id, "student_id": student_id,
                        "is_deleted": {"$ne": True},
                        "status": {"$nin": ["cancelled", "draft"]}}},
            {"$group": {"_id": None, "billed": {"$sum": "$total"},
                        "paid": {"$sum": "$paid_amount"}, "invoices": {"$sum": 1}}},
        ]
    ).to_list(length=1)
    data = rows[0] if rows else {"billed": 0, "paid": 0, "invoices": 0}
    billed = round(float(data.get("billed", 0)), 2)
    paid = round(float(data.get("paid", 0)), 2)
    overdue = await collection(C.FEE_INVOICES).count_documents(
        {"tenant_id": tenant_id, "student_id": student_id, "status": "overdue",
         "is_deleted": {"$ne": True}}
    )
    return {
        "invoices": data.get("invoices", 0),
        "billed": billed,
        "paid": paid,
        "outstanding": round(max(billed - paid, 0), 2),
        "overdue_invoices": overdue,
    }


# ── Promotion ─────────────────────────────────────────────────────────────
async def promote_students(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    student_ids: list[str],
    to_class_id: str,
    to_section_id: str | None,
    to_academic_year_id: str,
    result: str = "pass",
    reset_roll_numbers: bool = True,
) -> dict[str, Any]:
    """Move a cohort into the next year.

    The previous enrolment is closed rather than overwritten, so last year's
    class still resolves when an old report card or invoice is opened.
    """
    students = collection(C.STUDENTS)
    enrollments = Repository(C.ENROLLMENTS, tenant.id, actor_id=auth.user_id)
    to_class = ObjectId(to_class_id)
    to_section = ObjectId(to_section_id) if to_section_id else None
    to_year = ObjectId(to_academic_year_id)

    promoted, skipped = 0, []
    roll = 1
    for raw_id in student_ids:
        if not ObjectId.is_valid(raw_id):
            skipped.append({"id": raw_id, "reason": "invalid id"})
            continue
        sid = ObjectId(raw_id)
        student = await students.find_one(
            {"_id": sid, "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
        )
        if student is None:
            skipped.append({"id": raw_id, "reason": "not found"})
            continue
        if student.get("status") != "active":
            skipped.append({"id": raw_id, "reason": f"status is {student.get('status')}"})
            continue

        await enrollments.update_many(
            {"student_id": sid, "status": "active"},
            {"status": "promoted" if result == "pass" else "detained", "result": result},
        )
        new_roll = str(roll) if reset_roll_numbers else student.get("roll_number", "")
        await enrollments.create({
            "student_id": sid,
            "academic_year_id": to_year,
            "class_id": to_class,
            "section_id": to_section,
            "roll_number": new_roll,
            "enrolled_on": utcnow(),
            "status": "active",
        })
        await students.update_one(
            {"_id": sid},
            {"$set": {
                "current_class_id": to_class,
                "current_section_id": to_section,
                "academic_year_id": to_year,
                "roll_number": new_roll,
                "updated_at": utcnow(),
                "updated_by": auth.user_id,
            }},
        )
        promoted += 1
        roll += 1

    await recount_section(tenant.id, to_section)
    return {"promoted": promoted, "skipped": skipped,
            "detail": f"{promoted} student(s) promoted"}


async def change_status(
    tenant: TenantContext, auth: AuthContext, student_id: str, status: str, reason: str = ""
) -> dict[str, Any]:
    valid = {"active", "inactive", "graduated", "transferred", "dropped", "suspended"}
    if status not in valid:
        raise ValidationError(f"'{status}' is not a valid student status")
    students = collection(C.STUDENTS)
    before = await students.find_one(
        {"_id": ObjectId(student_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if before is None:
        raise NotFound("Student not found")
    await students.update_one(
        {"_id": before["_id"]},
        {"$set": {"status": status, "status_reason": reason, "status_changed_on": utcnow(),
                  "updated_at": utcnow(), "updated_by": auth.user_id}},
    )
    await recount_section(tenant.id, before.get("current_section_id"))
    # A student who has left keeps their login disabled rather than deleted.
    if status != "active":
        await collection(C.USERS).update_many(
            {"tenant_id": tenant.id, "student_id": before["_id"]},
            {"$set": {"is_active": False, "updated_at": utcnow()}},
        )
    return {"id": student_id, "status": status, "detail": f"Student marked {status}"}
