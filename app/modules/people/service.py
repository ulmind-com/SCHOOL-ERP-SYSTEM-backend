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
#: How many times a generator will step past an id that is already taken before
#: giving up. The counter is atomic, so a collision only happens when rows
#: arrived some other way — an import, a seed, or an id typed in by hand — and a
#: handful of steps clears any realistic overlap.
MAX_ID_ATTEMPTS = 50


async def _free_id(
    tenant_id: ObjectId, collection_name: str, field: str, build
) -> str:
    """Step the counter until the id it produces is genuinely unused.

    Without this, a school that imported its existing staff list gets a
    generator counting from one straight into ids that already exist, and every
    new record is rejected as a duplicate with nothing the user can do about it.
    """
    for _ in range(MAX_ID_ATTEMPTS):
        candidate = await build()
        taken = await collection(collection_name).find_one(
            {"tenant_id": tenant_id, field: candidate, "is_deleted": {"$ne": True}},
            projection={"_id": 1},
        )
        if taken is None:
            return candidate
    raise ValidationError(
        f"Could not allocate a free {field.replace('_', ' ')}. "
        "Set the numbering prefix in Settings, or enter one by hand."
    )


async def generate_admission_number(tenant: TenantContext) -> str:
    """``<prefix><year><0001>`` — configurable per institution, sequential and
    checked against the collection so an imported roll cannot block it."""
    conf = tenant.settings.get("admission_number", {}) if tenant.settings else {}
    prefix = conf.get("prefix", "ADM")
    include_year = conf.get("include_year", True)
    width = int(conf.get("width", 4))
    year = datetime.now(UTC).year
    key = f"admission:{year}" if include_year else "admission"

    async def build() -> str:
        seq = await next_sequence(tenant.id, key)
        parts = [prefix]
        if include_year:
            parts.append(str(year))
        parts.append(str(seq).zfill(width))
        return "".join(parts)

    return await _free_id(tenant.id, C.STUDENTS, "admission_number", build)


async def generate_employee_id(tenant: TenantContext) -> str:
    conf = tenant.settings.get("employee_id", {}) if tenant.settings else {}
    prefix = conf.get("prefix", "EMP")
    width = int(conf.get("width", 4))

    async def build() -> str:
        seq = await next_sequence(tenant.id, "employee")
        return f"{prefix}{str(seq).zfill(width)}"

    return await _free_id(tenant.id, C.STAFF, "employee_id", build)


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


async def assign_roll_numbers(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    section_id: str,
    order_by: str = "first_name",
    start_at: int = 1,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Number a section's students in one pass.

    Schools renumber every year and the order is a house rule — alphabetical is
    the common one, admission order the other. ``overwrite`` off leaves numbers
    already assigned alone, so a mid-year admission can be slotted in without
    disturbing the register everyone has already written in.
    """
    if order_by not in {"first_name", "last_name", "admission_number", "date_of_birth"}:
        raise ValidationError("Order by name, surname, admission number or date of birth")

    section_oid = ObjectId(section_id)
    students = await collection(C.STUDENTS).find({
        "tenant_id": tenant.id, "current_section_id": section_oid,
        "status": "active", "is_deleted": {"$ne": True},
    }).to_list(length=1000)
    if not students:
        raise ValidationError("That section has no active students")

    def sort_key(doc: dict) -> Any:
        value = doc.get(order_by)
        if order_by == "date_of_birth":
            return (value is None, value)
        return str(value or "").strip().lower()

    students.sort(key=sort_key)

    repo = Repository(C.STUDENTS, tenant.id, actor_id=auth.user_id)
    taken = {
        str(s.get("roll_number") or "")
        for s in students
        if not overwrite and s.get("roll_number")
    }
    number, changed, skipped = start_at, 0, 0
    assignments: list[dict[str, Any]] = []

    for student in students:
        if not overwrite and student.get("roll_number"):
            skipped += 1
            continue
        while str(number) in taken:
            number += 1
        roll = str(number)
        if student.get("roll_number") != roll:
            await repo.update(student["_id"], {"roll_number": roll})
            changed += 1
        assignments.append({
            "student_id": str(student["_id"]),
            "name": " ".join(filter(None, [student.get("first_name"),
                                           student.get("last_name")])),
            "roll_number": roll,
        })
        taken.add(roll)
        number += 1

    return {
        "section_id": section_id,
        "assigned": changed,
        "kept": skipped,
        "students": assignments,
        "detail": (
            f"{changed} roll number(s) assigned"
            + (f", {skipped} kept as they were" if skipped else "")
        ),
    }


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

    # Day-by-day attendance, for the calendar on the profile. A percentage
    # tells you there is a problem; the calendar tells you it is every Monday.
    out["attendance_days"] = await attendance_calendar(tenant.id, _id)

    # Fees
    out["fees"] = await fee_summary(tenant.id, _id)

    # Recent results
    cards = await collection(C.REPORT_CARDS).find(
        {"tenant_id": tenant.id, "student_id": _id, "is_deleted": {"$ne": True}}
    ).sort([("created_at", -1)]).limit(5).to_list(length=5)
    out["report_cards"] = [serialize_doc(c) for c in cards]

    # Marks, grouped by exam. Report cards only exist once results are
    # published; a parent checking after a class test wants to see the marks
    # that were entered, not an empty screen.
    out["exam_results"] = await exam_results(tenant.id, _id)

    # Files filed against this student — certificates, photographs, transfers.
    out["documents"] = [
        serialize_doc(d)
        for d in await collection(C.DOCUMENTS).find(
            {"tenant_id": tenant.id, "owner_type": "student", "owner_id": _id,
             "is_deleted": {"$ne": True}}
        ).sort([("created_at", -1)]).to_list(length=100)
    ]

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


async def attendance_calendar(
    tenant_id: ObjectId, student_id: ObjectId, days: int = 400
) -> list[dict[str, Any]]:
    """One entry per marked day, oldest first.

    Capped at a little over a year: that is what the calendar draws, and a
    student with ten years of history should not have all of it serialised into
    every profile request.
    """
    rows = await collection(C.ATTENDANCE).find(
        {"tenant_id": tenant_id, "student_id": student_id, "session_key": "day",
         "is_deleted": {"$ne": True}},
        projection={"date": 1, "status": 1, "remark": 1},
    ).sort([("date", -1)]).to_list(length=days)
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        date_value = row.get("date")
        if not date_value:
            continue
        key = (
            date_value.date().isoformat() if hasattr(date_value, "date")
            else str(date_value)[:10]
        )
        by_date[key] = {
            "date": key,
            "status": row.get("status", ""),
            "remark": row.get("remark", ""),
        }

    # Holidays carry no attendance row — nobody marked anything — so without
    # this the school's longest break reads as a stretch of blank squares.
    if by_date:
        await _overlay_holidays(tenant_id, student_id, by_date)

    return [by_date[key] for key in sorted(by_date)]


async def _overlay_holidays(
    tenant_id: ObjectId, student_id: ObjectId, by_date: dict[str, dict[str, Any]]
) -> None:
    """Paint the institution's holidays onto a student's calendar.

    Only inside the range the calendar already covers, and only where nothing
    was marked: a holiday the school taught through has a real register, and
    that register is the truth about the day.
    """
    from datetime import date as _date
    from datetime import timedelta

    student = await collection(C.STUDENTS).find_one(
        {"_id": student_id}, projection={"current_class_id": 1}
    )
    class_id = (student or {}).get("current_class_id")

    first = _date.fromisoformat(min(by_date))
    last = _date.fromisoformat(max(by_date))
    holidays = await collection(C.HOLIDAYS).find({
        "tenant_id": tenant_id, "is_active": True, "is_deleted": {"$ne": True},
    }).to_list(length=500)

    for holiday in holidays:
        classes = holiday.get("class_ids") or []
        if classes and class_id is not None and class_id not in classes:
            continue
        start = holiday["start_date"].date()
        end = (holiday.get("end_date") or holiday["start_date"]).date()
        day = max(start, first)
        while day <= min(end, last):
            key = day.isoformat()
            if key not in by_date:
                by_date[key] = {
                    "date": key,
                    "status": "holiday",
                    "remark": holiday.get("name", "Holiday"),
                }
            day += timedelta(days=1)


async def exam_results(tenant_id: ObjectId, student_id: ObjectId) -> list[dict[str, Any]]:
    """Every mark this student has, grouped under the exam it belongs to."""
    marks = await collection(C.MARKS).find(
        {"tenant_id": tenant_id, "student_id": student_id, "is_deleted": {"$ne": True}}
    ).to_list(length=1000)
    if not marks:
        return []

    exam_ids = {m.get("exam_id") for m in marks if m.get("exam_id")}
    subject_ids = {m.get("subject_id") for m in marks if m.get("subject_id")}
    exams = {
        e["_id"]: e
        for e in await collection(C.EXAMS).find({"_id": {"$in": list(exam_ids)}}).to_list(None)
    }
    subjects = {
        s["_id"]: s
        for s in await collection(C.SUBJECTS).find(
            {"_id": {"$in": list(subject_ids)}}
        ).to_list(None)
    }

    # Grades are stamped onto a mark when it is saved, but marks imported or
    # seeded before a scale existed have none. Filling one in for display costs
    # a lookup and saves a report card full of dashes.
    from app.modules.exams.service import _grade_scale_for, grade_for

    scales: dict[Any, dict | None] = {}

    grouped: dict[Any, dict[str, Any]] = {}
    for mark in marks:
        exam = exams.get(mark.get("exam_id")) or {}
        bucket = grouped.setdefault(mark.get("exam_id"), {
            "exam_id": str(mark.get("exam_id") or ""),
            "exam_name": exam.get("name", "Examination"),
            "exam_type": exam.get("type", ""),
            "status": exam.get("status", ""),
            "subjects": [],
            "obtained": 0.0,
            "max_marks": 0.0,
        })
        subject = subjects.get(mark.get("subject_id")) or {}
        obtained = mark.get("total_marks")
        if obtained is None:
            obtained = mark.get("marks_obtained")
        maximum = float(mark.get("max_marks") or 0)

        grade = mark.get("grade", "")
        percentage = mark.get("percentage")
        if not grade and percentage is not None:
            exam_key = mark.get("exam_id")
            if exam_key not in scales:
                scales[exam_key] = await _grade_scale_for(tenant_id, exam)
            grade = grade_for(scales[exam_key], float(percentage))[0]

        bucket["subjects"].append({
            "subject_id": str(mark.get("subject_id") or ""),
            "subject_name": subject.get("name", "Subject"),
            "code": subject.get("code", ""),
            "marks_obtained": obtained,
            "max_marks": maximum,
            "percentage": percentage,
            "grade": grade,
            "is_pass": mark.get("is_pass"),
        })
        if obtained is not None:
            bucket["obtained"] += float(obtained)
            bucket["max_marks"] += maximum

    out = []
    for bucket in grouped.values():
        bucket["subjects"].sort(key=lambda s: s["subject_name"])
        bucket["percentage"] = (
            round(bucket["obtained"] / bucket["max_marks"] * 100, 2)
            if bucket["max_marks"] else None
        )
        # Summing halves in binary gives 369.70000000000005; nobody wants that
        # on a report card.
        bucket["obtained"] = round(bucket["obtained"], 2)
        bucket["max_marks"] = round(bucket["max_marks"], 2)
        out.append(bucket)
    out.sort(key=lambda b: b["exam_name"])
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
    # A holiday is not a day the child missed. Leaving it in the denominator is
    # how someone with a perfect record ends up reading as eighty per cent.
    from app.modules.attendance.service import NON_TEACHING

    teaching = {k: v for k, v in counts.items() if k not in NON_TEACHING}
    total = sum(teaching.values())
    present = (
        teaching.get("present", 0)
        + teaching.get("late", 0)
        + teaching.get("half_day", 0) * 0.5
    )
    return {
        "total_days": total,
        "present": counts.get("present", 0),
        "absent": counts.get("absent", 0),
        "late": counts.get("late", 0),
        "leave": counts.get("leave", 0),
        "holiday": counts.get("holiday", 0),
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
