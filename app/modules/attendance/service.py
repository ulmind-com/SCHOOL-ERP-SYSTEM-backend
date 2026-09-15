"""Taking and reading attendance."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo import UpdateOne

from app.core.context import AuthContext, TenantContext
from app.core.exceptions import Forbidden, NotFound, ValidationError
from app.db.mongo import C, collection
from app.models.base import serialize_doc, utcnow

PRESENT_LIKE = {"present", "late", "half_day"}
VALID_STATUS = {"present", "absent", "late", "half_day", "excused", "leave", "holiday"}


def day_bounds(on: date) -> tuple[datetime, datetime]:
    start = datetime(on.year, on.month, on.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def as_datetime(on: date) -> datetime:
    return datetime(on.year, on.month, on.day, tzinfo=UTC)


#: Statuses that mean "no teaching happened", so they belong in neither half of
#: an attendance percentage. Counting a holiday as a day the child missed is how
#: a 100% attender ends up looking like an 80% one.
NON_TEACHING = {"holiday", "excused"}


async def holidays_between(
    tenant: TenantContext, start: date, end: date, class_id: ObjectId | None = None
) -> list[dict[str, Any]]:
    """Every holiday overlapping the range, narrowed to a class if given."""
    query: dict[str, Any] = {
        "tenant_id": tenant.id,
        "is_active": True,
        "is_deleted": {"$ne": True},
        "start_date": {"$lte": as_datetime(end)},
        "$or": [
            {"end_date": {"$gte": as_datetime(start)}},
            {"end_date": None},
            {"end_date": {"$exists": False}},
        ],
    }
    rows = await collection(C.HOLIDAYS).find(query).sort([("start_date", 1)]).to_list(
        length=500
    )
    out = []
    for row in rows:
        classes = row.get("class_ids") or []
        # No classes named means the whole institution is off.
        if classes and class_id is not None and class_id not in classes:
            continue
        first = row["start_date"].date()
        last = (row.get("end_date") or row["start_date"]).date()
        if last < start or first > end:
            continue
        out.append({**row, "_first": first, "_last": last})
    return out


async def holiday_on(
    tenant: TenantContext, on: date, class_id: ObjectId | None = None
) -> dict[str, Any] | None:
    """The holiday covering this date, or ``None``.

    When several overlap — a festival inside a vacation — the one that still
    requires a register wins, because that is the stricter statement about
    whether the school is open.
    """
    found = await holidays_between(tenant, on, on, class_id)
    if not found:
        return None
    found.sort(key=lambda h: not h.get("attendance_required"))
    return found[0]


async def holiday_conflicts(tenant: TenantContext, holiday_id: str) -> dict[str, Any]:
    """Attendance already recorded on days this holiday now covers.

    Schools declare a holiday after the fact — a bandh, a cremation, a day the
    board moved. The registers for those days already exist and say children
    attended, which is both untrue and counted against everyone's percentage.
    """
    holiday = await collection(C.HOLIDAYS).find_one(
        {"_id": ObjectId(holiday_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if holiday is None:
        raise NotFound("Holiday not found")

    start = holiday["start_date"]
    end = holiday.get("end_date") or holiday["start_date"]
    query: dict[str, Any] = {
        "tenant_id": tenant.id,
        "date": {"$gte": as_datetime(start.date()), "$lte": as_datetime(end.date())},
        "status": {"$nin": list(NON_TEACHING)},
        "is_deleted": {"$ne": True},
    }
    classes = holiday.get("class_ids") or []
    if classes:
        query["class_id"] = {"$in": classes}

    records = await collection(C.ATTENDANCE).count_documents(query)
    dates = await collection(C.ATTENDANCE).distinct("date", query)
    return {
        "holiday": {"id": str(holiday["_id"]), "name": holiday.get("name", ""),
                    "attendance_required": bool(holiday.get("attendance_required"))},
        "records": records,
        "dates": sorted(d.date().isoformat() for d in dates),
        "detail": (
            f"{records} attendance record(s) were taken on "
            f"{len(dates)} day(s) this holiday now covers."
            if records else "No attendance was recorded on these days."
        ),
    }


async def release_holiday_attendance(
    tenant: TenantContext, auth: AuthContext, holiday_id: str
) -> dict[str, Any]:
    """Restate those records as holiday, rather than deleting them.

    The register was genuinely taken — someone stood in a room — so erasing it
    loses a fact. Restating it keeps the row and takes the day out of the
    percentage, which is what the school actually meant.
    """
    conflicts = await holiday_conflicts(tenant, holiday_id)
    if conflicts["holiday"]["attendance_required"]:
        raise ValidationError(
            "That holiday is marked as one where attendance is still taken. "
            "Turn that off first if the institution was closed."
        )
    if not conflicts["records"]:
        return {**conflicts, "updated": 0}

    holiday = await collection(C.HOLIDAYS).find_one({"_id": ObjectId(holiday_id)})
    start = holiday["start_date"]
    end = holiday.get("end_date") or holiday["start_date"]
    query: dict[str, Any] = {
        "tenant_id": tenant.id,
        "date": {"$gte": as_datetime(start.date()), "$lte": as_datetime(end.date())},
        "status": {"$nin": list(NON_TEACHING)},
        "is_deleted": {"$ne": True},
    }
    classes = holiday.get("class_ids") or []
    if classes:
        query["class_id"] = {"$in": classes}

    result = await collection(C.ATTENDANCE).update_many(
        query,
        {"$set": {"status": "holiday", "remark": holiday.get("name", "Holiday"),
                  "updated_at": utcnow(), "updated_by": auth.user_id}},
    )
    return {
        **conflicts,
        "updated": result.modified_count,
        "detail": (
            f"{result.modified_count} record(s) restated as {holiday.get('name', 'holiday')}. "
            "Those days no longer count for or against anyone."
        ),
    }


async def get_register(
    tenant: TenantContext,
    *,
    section_id: str,
    on: date,
    session_key: str = "day",
    only_students: list[ObjectId] | None = None,
) -> dict[str, Any]:
    """The roster with whatever has already been marked, so the UI opens
    pre-filled rather than blank on a correction.

    ``only_students`` narrows the roster for a family portal. A parent holds
    attendance:read for their own child; without this the same permission handed
    them every classmate's name and whether they turned up.
    """
    sid = ObjectId(section_id)
    section = await collection(C.SECTIONS).find_one({"_id": sid, "tenant_id": tenant.id})
    if section is None:
        raise NotFound("Section not found")

    roster_query: dict[str, Any] = {
        "tenant_id": tenant.id, "current_section_id": sid, "status": "active",
        "is_deleted": {"$ne": True},
    }
    if only_students is not None:
        roster_query["_id"] = {"$in": only_students}

    students = await collection(C.STUDENTS).find(
        roster_query,
        {"first_name": 1, "middle_name": 1, "last_name": 1, "roll_number": 1,
         "admission_number": 1, "photo": 1},
    ).sort([("roll_number", 1)]).to_list(length=500)

    existing = await collection(C.ATTENDANCE).find(
        {"tenant_id": tenant.id, "section_id": sid, "date": as_datetime(on),
         "session_key": session_key, "is_deleted": {"$ne": True}}
    ).to_list(length=500)
    marked = {r["student_id"]: r for r in existing}

    holiday = await holiday_on(tenant, on, section.get("class_id"))

    session = await collection(C.ATTENDANCE_SESSIONS).find_one(
        {"tenant_id": tenant.id, "section_id": sid, "date": as_datetime(on),
         "session_key": session_key}
    )

    summary = serialize_doc(session) if session else None
    if summary:
        total = summary.get("total") or 0
        present_like = sum(summary.get(k, 0) for k in ("present", "late"))
        summary["percentage"] = round(present_like / total * 100, 2) if total else 0.0

    rows = []
    for student in students:
        record = marked.get(student["_id"])
        rows.append({
            "student_id": str(student["_id"]),
            "full_name": " ".join(
                p for p in (student.get("first_name"), student.get("middle_name"),
                            student.get("last_name")) if p
            ),
            "roll_number": student.get("roll_number", ""),
            "admission_number": student.get("admission_number", ""),
            "photo": (student.get("photo") or {}).get("url", ""),
            "status": record.get("status") if record else None,
            "remark": record.get("remark", "") if record else "",
            "in_time": record.get("in_time", "") if record else "",
        })

    return {
        "section": serialize_doc(section),
        "date": on.isoformat(),
        "session_key": session_key,
        "already_taken": session is not None,
        "is_locked": bool((session or {}).get("is_locked")),
        "summary": summary,
        "students": rows,
        # A holiday the school still teaches on — a founder's day, a sports day
        # — is a normal register with a note on it; one it is shut for is not a
        # register at all.
        "holiday": {
            "id": str(holiday["_id"]),
            "name": holiday.get("name", ""),
            "type": holiday.get("type", "public"),
            "description": holiday.get("description", ""),
            "attendance_required": bool(holiday.get("attendance_required")),
            "start_date": holiday["_first"].isoformat(),
            "end_date": holiday["_last"].isoformat(),
        } if holiday else None,
        "can_take": holiday is None or bool(holiday.get("attendance_required")),
    }


async def take_register(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    section_id: str,
    on: date,
    entries: list[dict[str, Any]],
    session_key: str = "day",
    subject_id: str | None = None,
    period_id: str | None = None,
    notes: str = "",
    despite_holiday: bool = False,
) -> dict[str, Any]:
    """Write a whole register in one round trip.

    Upserts rather than inserts, so re-submitting a corrected register updates
    the existing rows instead of failing on the unique index.
    """
    if not entries:
        raise ValidationError("No attendance to save")
    sid = ObjectId(section_id)
    section = await collection(C.SECTIONS).find_one({"_id": sid, "tenant_id": tenant.id})
    if section is None:
        raise NotFound("Section not found")

    # A school that is shut has no register to take. Refusing here rather than
    # only in the browser is what stops a holiday quietly becoming a day forty
    # children were marked absent.
    holiday = await holiday_on(tenant, on, section.get("class_id"))
    if holiday and not holiday.get("attendance_required") and not despite_holiday:
        raise ValidationError(
            f"{on.isoformat()} is {holiday.get('name', 'a holiday')} and the register is "
            "closed. Turn on 'attendance is taken' for that holiday, or confirm you are "
            "taking it anyway."
        )

    existing_session = await collection(C.ATTENDANCE_SESSIONS).find_one(
        {"tenant_id": tenant.id, "section_id": sid, "date": as_datetime(on),
         "session_key": session_key}
    )
    if existing_session and existing_session.get("is_locked") and not auth.can("attendance:delete"):
        raise Forbidden("This register has been locked. Ask an administrator to reopen it.")

    when = as_datetime(on)
    now = utcnow()
    counts = {"present": 0, "absent": 0, "late": 0, "leave": 0, "half_day": 0, "excused": 0}
    operations: list[UpdateOne] = []

    for entry in entries:
        raw_id = entry.get("student_id")
        if not raw_id or not ObjectId.is_valid(raw_id):
            continue
        status = (entry.get("status") or "present").lower()
        if status not in VALID_STATUS:
            raise ValidationError(f"'{status}' is not a valid attendance status")
        counts[status] = counts.get(status, 0) + 1
        operations.append(
            UpdateOne(
                {"tenant_id": tenant.id, "student_id": ObjectId(raw_id), "date": when,
                 "session_key": session_key},
                {
                    "$set": {
                        "section_id": sid,
                        "class_id": section.get("class_id"),
                        "academic_year_id": section.get("academic_year_id"),
                        "subject_id": ObjectId(subject_id) if subject_id else None,
                        "status": status,
                        "remark": entry.get("remark", ""),
                        "in_time": entry.get("in_time", ""),
                        "minutes_late": int(entry.get("minutes_late") or 0),
                        "marked_by": auth.user_id,
                        "updated_at": now,
                        "updated_by": auth.user_id,
                    },
                    "$setOnInsert": {"created_at": now, "created_by": auth.user_id,
                                     "is_deleted": False},
                },
                upsert=True,
            )
        )

    if not operations:
        raise ValidationError("No valid students in the submission")
    result = await collection(C.ATTENDANCE).bulk_write(operations, ordered=False)

    total = sum(counts.values())
    present_like = sum(counts.get(s, 0) for s in PRESENT_LIKE)
    await collection(C.ATTENDANCE_SESSIONS).update_one(
        {"tenant_id": tenant.id, "section_id": sid, "date": when, "session_key": session_key},
        {
            "$set": {
                "class_id": section.get("class_id"),
                "academic_year_id": section.get("academic_year_id"),
                "subject_id": ObjectId(subject_id) if subject_id else None,
                "period_id": ObjectId(period_id) if period_id else None,
                "taken_by": auth.user_id,
                "taken_at": now,
                "total": total,
                "present": counts.get("present", 0),
                "absent": counts.get("absent", 0),
                "late": counts.get("late", 0),
                "on_leave": counts.get("leave", 0),
                "notes": notes,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now, "is_deleted": False, "is_locked": False},
        },
        upsert=True,
    )

    return {
        "saved": len(operations),
        "created": result.upserted_count,
        "updated": result.modified_count,
        "counts": counts,
        "percentage": round(present_like / total * 100, 2) if total else 0.0,
        "detail": f"Attendance saved for {total} student(s)",
    }


async def section_summary(
    tenant: TenantContext, *, section_id: str | None, class_id: str | None,
    start: date, end: date,
) -> dict[str, Any]:
    match: dict[str, Any] = {
        "tenant_id": tenant.id,
        "session_key": "day",
        "is_deleted": {"$ne": True},
        "date": {"$gte": as_datetime(start), "$lte": as_datetime(end)},
    }
    if section_id:
        match["section_id"] = ObjectId(section_id)
    if class_id:
        match["class_id"] = ObjectId(class_id)

    by_status = await collection(C.ATTENDANCE).aggregate(
        [{"$match": match}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]
    ).to_list(length=None)
    by_day = await collection(C.ATTENDANCE).aggregate(
        [
            {"$match": match},
            {"$group": {
                "_id": "$date",
                "total": {"$sum": 1},
                "present": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_LIKE)]}, 1, 0]}},
            }},
            {"$sort": {"_id": 1}},
        ]
    ).to_list(length=None)

    counts = {r["_id"]: r["n"] for r in by_status}
    total = sum(counts.values())
    present = sum(counts.get(s, 0) for s in PRESENT_LIKE)
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "totals": counts,
        "records": total,
        "percentage": round(present / total * 100, 2) if total else 0.0,
        "daily": [
            {
                "date": r["_id"].date().isoformat(),
                "total": r["total"],
                "present": r["present"],
                "percentage": round(r["present"] / r["total"] * 100, 2) if r["total"] else 0.0,
            }
            for r in by_day
        ],
    }


async def student_calendar(
    tenant: TenantContext, student_id: str, *, year: int, month: int
) -> dict[str, Any]:
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    rows = await collection(C.ATTENDANCE).find(
        {"tenant_id": tenant.id, "student_id": ObjectId(student_id), "session_key": "day",
         "is_deleted": {"$ne": True},
         "date": {"$gte": as_datetime(start), "$lte": as_datetime(end)}}
    ).sort([("date", 1)]).to_list(length=40)

    days = {
        r["date"].date().isoformat(): {"status": r.get("status"), "remark": r.get("remark", "")}
        for r in rows
    }
    present = sum(1 for r in rows if r.get("status") in PRESENT_LIKE)
    return {
        "year": year, "month": month,
        "days": days,
        "marked_days": len(rows),
        "present_days": present,
        "percentage": round(present / len(rows) * 100, 2) if rows else 0.0,
    }


async def defaulters(
    tenant: TenantContext, *, threshold: float = 75, class_id: str | None = None,
    section_id: str | None = None, start: date | None = None, end: date | None = None,
) -> list[dict[str, Any]]:
    """Students below an attendance threshold — the list a school actually
    needs before sending letters home."""
    match: dict[str, Any] = {"tenant_id": tenant.id, "session_key": "day",
                             "is_deleted": {"$ne": True}}
    if class_id:
        match["class_id"] = ObjectId(class_id)
    if section_id:
        match["section_id"] = ObjectId(section_id)
    if start and end:
        match["date"] = {"$gte": as_datetime(start), "$lte": as_datetime(end)}

    rows = await collection(C.ATTENDANCE).aggregate([
        {"$match": match},
        {"$group": {
            "_id": "$student_id",
            "total": {"$sum": 1},
            "present": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_LIKE)]}, 1, 0]}},
        }},
        {"$project": {
            "total": 1, "present": 1,
            "percentage": {
                "$cond": [{"$eq": ["$total", 0]}, 0,
                          {"$multiply": [{"$divide": ["$present", "$total"]}, 100]}],
            },
        }},
        {"$match": {"percentage": {"$lt": threshold}}},
        {"$sort": {"percentage": 1}},
        {"$limit": 500},
    ]).to_list(length=500)

    student_ids = [r["_id"] for r in rows]
    students = {
        s["_id"]: s
        for s in await collection(C.STUDENTS).find(
            {"_id": {"$in": student_ids}},
            {"first_name": 1, "last_name": 1, "roll_number": 1, "admission_number": 1,
             "current_class_id": 1, "current_section_id": 1, "contact": 1},
        ).to_list(length=None)
    }
    out = []
    for row in rows:
        student = students.get(row["_id"], {})
        out.append({
            "student_id": str(row["_id"]),
            "full_name": " ".join(
                p for p in (student.get("first_name"), student.get("last_name")) if p
            ),
            "roll_number": student.get("roll_number", ""),
            "admission_number": student.get("admission_number", ""),
            "phone": (student.get("contact") or {}).get("phone", ""),
            "total_days": row["total"],
            "present_days": row["present"],
            "percentage": round(row["percentage"], 2),
        })
    return out


async def set_lock(
    tenant: TenantContext, *, section_id: str, on: date, session_key: str, locked: bool
) -> dict[str, Any]:
    result = await collection(C.ATTENDANCE_SESSIONS).update_one(
        {"tenant_id": tenant.id, "section_id": ObjectId(section_id), "date": as_datetime(on),
         "session_key": session_key},
        {"$set": {"is_locked": locked, "updated_at": utcnow()}},
    )
    if result.matched_count == 0:
        raise NotFound("No attendance has been taken for that day")
    return {"locked": locked, "detail": "Register " + ("locked" if locked else "reopened")}
