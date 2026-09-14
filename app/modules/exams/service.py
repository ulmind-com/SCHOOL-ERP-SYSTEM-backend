"""Marking schemes and the re-grading that has to follow a change to one."""

from typing import Any

from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models.base import serialize_doc, utcnow

#: Used when neither the exam schedule nor the subject says otherwise.
DEFAULT_MAX_MARKS = 100.0
DEFAULT_PASS_MARKS = 33.0


async def _grade_scale_for(tenant_id: ObjectId, exam: dict | None) -> dict | None:
    """The exam's own scale, else whichever the institution marked default."""
    scale_id = (exam or {}).get("grade_scale_id")
    if scale_id:
        found = await collection(C.GRADE_SCALES).find_one(
            {"_id": scale_id, "tenant_id": tenant_id, "is_deleted": {"$ne": True}}
        )
        if found:
            return found
    return await collection(C.GRADE_SCALES).find_one(
        {"tenant_id": tenant_id, "is_default": True, "is_deleted": {"$ne": True}}
    )


def grade_for(scale: dict | None, percentage: float) -> tuple[str, float, str]:
    """``(grade, points, remark)`` for a percentage, blanks when no scale applies."""
    for band in (scale or {}).get("bands", []):
        if float(band.get("min", 0)) <= percentage <= float(band.get("max", 100)):
            return (
                str(band.get("grade", "")),
                float(band.get("points", 0) or 0),
                str(band.get("remark", "")),
            )
    return "", 0.0, ""


async def marking_scheme(
    tenant: TenantContext, exam_id: str, subject_id: str
) -> dict[str, Any]:
    """What this subject is marked out of in this exam, and what passes it.

    Resolved in order: the exam schedule row, then the subject's own defaults,
    then the house defaults — so a school that never opens this screen still
    gets sensible numbers, and one that does gets them per exam.
    """
    exam_oid, subject_oid = ObjectId(exam_id), ObjectId(subject_id)
    schedule = await collection(C.EXAM_SCHEDULES).find_one(
        {"tenant_id": tenant.id, "exam_id": exam_oid, "subject_id": subject_oid,
         "is_deleted": {"$ne": True}}
    )
    subject = await collection(C.SUBJECTS).find_one(
        {"_id": subject_oid, "tenant_id": tenant.id}
    )
    exam = await collection(C.EXAMS).find_one({"_id": exam_oid, "tenant_id": tenant.id})
    scale = await _grade_scale_for(tenant.id, exam)

    source = schedule or subject or {}
    max_marks = float(source.get("max_marks") or DEFAULT_MAX_MARKS)
    pass_marks = float(source.get("pass_marks") or DEFAULT_PASS_MARKS)
    return {
        "exam_id": exam_id,
        "subject_id": subject_id,
        "max_marks": max_marks,
        "pass_marks": pass_marks,
        "pass_percentage": round(pass_marks / max_marks * 100, 2) if max_marks else 0.0,
        "from_schedule": schedule is not None,
        "exam_schedule_id": str(schedule["_id"]) if schedule else None,
        "grade_scale": serialize_doc(scale) if scale else None,
    }


async def set_marking_scheme(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    exam_id: str,
    subject_id: str,
    max_marks: float,
    pass_marks: float,
) -> dict[str, Any]:
    """Store the scheme and bring every mark already entered back in line.

    Changing what a paper is out of after marks are in silently invalidates
    every percentage, grade and pass flag derived from the old figure. Rather
    than leave that for someone to discover on a report card, the marks are
    recomputed here in the same call.
    """
    exam_oid, subject_oid = ObjectId(exam_id), ObjectId(subject_id)
    schedules = Repository(C.EXAM_SCHEDULES, tenant.id, actor_id=auth.user_id)
    existing = await collection(C.EXAM_SCHEDULES).find_one(
        {"tenant_id": tenant.id, "exam_id": exam_oid, "subject_id": subject_oid,
         "is_deleted": {"$ne": True}}
    )
    payload = {"max_marks": float(max_marks), "pass_marks": float(pass_marks)}
    if existing:
        await schedules.update(existing["_id"], payload)
    else:
        subject = await collection(C.SUBJECTS).find_one(
            {"_id": subject_oid, "tenant_id": tenant.id}
        )
        await schedules.create({
            "exam_id": exam_oid,
            "subject_id": subject_oid,
            "class_id": (subject or {}).get("class_id"),
            "date": utcnow(),
            **payload,
        })

    regraded = await regrade(tenant, exam_id=exam_id, subject_id=subject_id)
    return {**await marking_scheme(tenant, exam_id, subject_id), "regraded": regraded,
            "detail": (
                f"Marked out of {money_ish(max_marks)}, pass at {money_ish(pass_marks)}"
                + (f" — {regraded} mark(s) recalculated" if regraded else "")
            )}


def money_ish(value: float) -> str:
    """Whole numbers without a trailing ``.0`` — marks are usually integers."""
    return str(int(value)) if float(value).is_integer() else str(value)


async def regrade(
    tenant: TenantContext, *, exam_id: str, subject_id: str | None = None
) -> int:
    """Recompute percentage, grade and pass flag against the current scheme."""
    query: dict[str, Any] = {
        "tenant_id": tenant.id, "exam_id": ObjectId(exam_id), "is_deleted": {"$ne": True},
    }
    if subject_id:
        query["subject_id"] = ObjectId(subject_id)

    exam = await collection(C.EXAMS).find_one(
        {"_id": ObjectId(exam_id), "tenant_id": tenant.id}
    )
    scale = await _grade_scale_for(tenant.id, exam)

    schemes: dict[ObjectId, dict] = {}
    marks = collection(C.MARKS)
    changed = 0
    async for mark in marks.find(query):
        subject_oid = mark.get("subject_id")
        if subject_oid not in schemes:
            schemes[subject_oid] = await marking_scheme(
                tenant, exam_id, str(subject_oid)
            )
        scheme = schemes[subject_oid]
        obtained = mark.get("total_marks")
        if obtained is None:
            obtained = mark.get("marks_obtained")
        if obtained is None:
            continue
        max_marks = float(scheme["max_marks"]) or DEFAULT_MAX_MARKS
        percentage = round(float(obtained) / max_marks * 100, 2)
        grade, points, remark = grade_for(scale, percentage)
        update = {
            "max_marks": max_marks,
            "percentage": percentage,
            "is_pass": float(obtained) >= float(scheme["pass_marks"]),
            "grade": grade,
            "grade_points": points,
            "remark": remark or mark.get("remark", ""),
        }
        if any(mark.get(k) != v for k, v in update.items()):
            await marks.update_one({"_id": mark["_id"]}, {"$set": update})
            changed += 1
    return changed
