"""Online classes and homework submission.

Online classes are *links with a schedule*, not a video platform. Schools
already run Zoom, Meet or Teams; what they lack is one place where the right
students see the right link at the right time, and where "did they join?" has
an answer. Hosting video ourselves would be a different product.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.core.exceptions import Conflict, Forbidden, NotFound, ValidationError
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models.base import serialize_doc, utcnow

#: A link is shown this long before it starts, and stays live this long after.
JOIN_WINDOW_BEFORE = timedelta(minutes=15)
JOIN_WINDOW_AFTER = timedelta(hours=3)


# ── Online classes ────────────────────────────────────────────────────────
async def schedule_class(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    title: str,
    section_ids: list[str],
    starts_at: datetime,
    duration_minutes: int = 45,
    subject_id: str | None = None,
    meeting_url: str = "",
    meeting_id: str = "",
    passcode: str = "",
    platform: str = "other",
    description: str = "",
) -> dict[str, Any]:
    if not meeting_url.strip():
        raise ValidationError("Paste the meeting link students should join")
    if not section_ids:
        raise ValidationError("Choose at least one section")

    sections = [ObjectId(s) for s in section_ids if ObjectId.is_valid(s)]
    ends_at = starts_at + timedelta(minutes=duration_minutes)

    classes = Repository(C.ONLINE_CLASSES, tenant.id, actor_id=auth.user_id)

    # Two live classes for the same section at the same time is a scheduling
    # mistake, not something to discover when nobody turns up.
    clash = await classes.find_one({
        "section_ids": {"$in": sections},
        "status": {"$ne": "cancelled"},
        "starts_at": {"$lt": ends_at},
        "ends_at": {"$gt": starts_at},
    })
    if clash:
        raise Conflict(
            f"'{clash.get('title', '')}' is already scheduled for that section at that time"
        )

    created = await classes.create({
        "title": title.strip(),
        "description": description,
        "subject_id": ObjectId(subject_id) if subject_id and ObjectId.is_valid(subject_id) else None,
        "section_ids": sections,
        "staff_id": auth.staff_id,
        "host_name": auth.full_name,
        "platform": platform,
        "meeting_url": meeting_url.strip(),
        "meeting_id": meeting_id,
        "passcode": passcode,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "duration_minutes": duration_minutes,
        "status": "scheduled",
        "attendees": [],
        "recording_url": "",
    })

    await _notify_class(tenant, created, sections)
    return {**(serialize_doc(created) or {}), "detail": "Class scheduled"}


async def _notify_class(
    tenant: TenantContext, online_class: dict[str, Any], sections: list[ObjectId]
) -> None:
    from app.modules.communication.notify import notify_audience

    await notify_audience(
        tenant,
        audience={"section_ids": [str(s) for s in sections]},
        title=f"Online class scheduled: {online_class.get('title', '')}",
        body=(
            f"{online_class['starts_at']:%d %b, %I:%M %p} · "
            f"{online_class.get('duration_minutes', 45)} minutes"
        ),
        category="class",
        link="/academics/live-classes",
    )


def join_state(online_class: dict[str, Any], now: datetime | None = None) -> str:
    """Whether the link should be handed out yet."""
    now = now or utcnow()
    starts = online_class["starts_at"]
    ends = online_class.get("ends_at") or starts + timedelta(minutes=45)
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=UTC)
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=UTC)

    if online_class.get("status") == "cancelled":
        return "cancelled"
    if now < starts - JOIN_WINDOW_BEFORE:
        return "upcoming"
    if now > ends + JOIN_WINDOW_AFTER:
        return "ended"
    return "live" if starts <= now <= ends else "joinable"


async def upcoming_classes(
    tenant: TenantContext, auth: AuthContext, *, days: int = 7
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {
        "tenant_id": tenant.id,
        "is_deleted": {"$ne": True},
        "starts_at": {"$gte": utcnow() - timedelta(hours=3),
                      "$lte": utcnow() + timedelta(days=days)},
    }

    # Students and parents see only their own section's classes.
    section_ids = await _sections_for(tenant, auth)
    if section_ids is not None:
        query["section_ids"] = {"$in": section_ids}
    elif auth.staff_id and not auth.can("lms:update"):
        query["staff_id"] = auth.staff_id

    docs = await collection(C.ONLINE_CLASSES).find(query).sort(
        [("starts_at", 1)]
    ).to_list(length=200)

    subjects = {
        s["_id"]: s.get("name", "")
        for s in await collection(C.SUBJECTS).find({"tenant_id": tenant.id}).to_list(None)
    }

    out = []
    for doc in docs:
        state = join_state(doc)
        row = serialize_doc(doc) or {}
        row["subject_name"] = subjects.get(doc.get("subject_id"), "")
        row["join_state"] = state
        row["attendee_count"] = len(doc.get("attendees") or [])
        # Do not hand out a link before it is usable — it only invites people
        # to sit in an empty room and then stop trusting the schedule.
        if state in {"upcoming", "ended", "cancelled"}:
            row["meeting_url"] = ""
            row["passcode"] = ""
        out.append(row)
    return out


async def _sections_for(
    tenant: TenantContext, auth: AuthContext
) -> list[ObjectId] | None:
    """The sections a family member may see, or None for staff."""
    if auth.student_id:
        student = await collection(C.STUDENTS).find_one(
            {"_id": auth.student_id, "tenant_id": tenant.id}
        )
        return [student["current_section_id"]] if (student or {}).get("current_section_id") else []
    if auth.guardian_id:
        guardian = await collection(C.GUARDIANS).find_one(
            {"_id": auth.guardian_id, "tenant_id": tenant.id}
        )
        students = await collection(C.STUDENTS).find(
            {"_id": {"$in": (guardian or {}).get("student_ids") or []}},
            {"current_section_id": 1},
        ).to_list(length=None)
        return [s["current_section_id"] for s in students if s.get("current_section_id")]
    return None


async def join(tenant: TenantContext, auth: AuthContext, class_id: str) -> dict[str, Any]:
    online_class = await collection(C.ONLINE_CLASSES).find_one(
        {"_id": ObjectId(class_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if online_class is None:
        raise NotFound("Class not found")

    state = join_state(online_class)
    if state in {"upcoming"}:
        raise Conflict("This class has not opened yet")
    if state in {"ended", "cancelled"}:
        raise Conflict(f"This class has {state}")

    section_ids = await _sections_for(tenant, auth)
    if section_ids is not None and not set(section_ids) & set(online_class.get("section_ids") or []):
        raise Forbidden("This class is not for your section")

    await collection(C.ONLINE_CLASSES).update_one(
        {"_id": online_class["_id"]},
        {"$addToSet": {"attendees": {"user_id": auth.user_id, "name": auth.full_name,
                                     "joined_at": utcnow()}}},
    )
    return {
        "meeting_url": online_class.get("meeting_url", ""),
        "meeting_id": online_class.get("meeting_id", ""),
        "passcode": online_class.get("passcode", ""),
        "title": online_class.get("title", ""),
    }


async def cancel_class(
    tenant: TenantContext, auth: AuthContext, class_id: str, reason: str = ""
) -> dict[str, Any]:
    classes = Repository(C.ONLINE_CLASSES, tenant.id, actor_id=auth.user_id)
    online_class = await classes.get_or_404(class_id, label="Class")
    updated = await classes.update(class_id, {
        "status": "cancelled", "cancel_reason": reason, "cancelled_at": utcnow(),
    })
    from app.modules.communication.notify import notify_audience

    await notify_audience(
        tenant,
        audience={"section_ids": [str(s) for s in online_class.get("section_ids") or []]},
        title=f"Class cancelled: {online_class.get('title', '')}",
        body=reason or "Please check the timetable for a replacement.",
        category="class",
    )
    return {**(serialize_doc(updated) or {}), "detail": "Class cancelled and students notified"}


# ── Homework submission ───────────────────────────────────────────────────
async def submit_homework(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    assignment_id: str,
    text_answer: str = "",
    attachments: list[dict] | None = None,
) -> dict[str, Any]:
    if not auth.student_id:
        raise Forbidden("Only a student can submit homework")
    if not text_answer.strip() and not attachments:
        raise ValidationError("Write an answer or attach a file")

    assignment = await collection(C.ASSIGNMENTS).find_one({
        "_id": ObjectId(assignment_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True},
    })
    if assignment is None:
        raise NotFound("Assignment not found")
    if assignment.get("status") != "published":
        raise Conflict("This assignment is not open for submission")
    if assignment.get("submission_mode", "online") == "offline":
        raise Conflict(
            "This one is collected in class — hand it to your teacher, "
            "who will mark it received"
        )

    due = assignment.get("due_date")
    late = bool(due and utcnow() > due.replace(tzinfo=UTC))
    if late and not assignment.get("allow_late_submission", True):
        raise Conflict("The deadline has passed and late submissions are not accepted")

    submissions = Repository(C.SUBMISSIONS, tenant.id, actor_id=auth.user_id)
    existing = await submissions.find_one({
        "assignment_id": assignment["_id"], "student_id": auth.student_id,
    })
    if existing and existing.get("status") == "graded":
        raise Conflict("This has already been graded and cannot be resubmitted")

    payload = {
        "assignment_id": assignment["_id"],
        "student_id": auth.student_id,
        "submitted_at": utcnow(),
        "status": "late" if late else "submitted",
        "text_answer": text_answer.strip(),
        "attachments": attachments or [],
        "collected_offline": False,
        "collected_by": None,
    }

    if existing:
        submission = await submissions.update(str(existing["_id"]), payload)
    else:
        submission = await submissions.create(payload)
        await collection(C.ASSIGNMENTS).update_one(
            {"_id": assignment["_id"]}, {"$inc": {"submission_count": 1}}
        )

    return {
        **(serialize_doc(submission) or {}),
        "detail": "Submitted" + (" (late)" if late else ""),
    }


async def expected_students(
    tenant: TenantContext, assignment: dict[str, Any]
) -> list[dict[str, Any]]:
    """Everyone the assignment reaches.

    Sections when it names them, the whole class when it does not — the same
    reach ``_assignments_for_student`` reads from the other end, so the roster a
    teacher ticks off is exactly the set of students who were shown the work.
    """
    query: dict[str, Any] = {
        "tenant_id": tenant.id, "status": "active", "is_deleted": {"$ne": True},
    }
    sections = assignment.get("section_ids") or []
    if sections:
        query["current_section_id"] = {"$in": sections}
    elif assignment.get("class_id"):
        query["current_class_id"] = assignment["class_id"]
    else:
        return []

    return await collection(C.STUDENTS).find(query, {
        "first_name": 1, "last_name": 1, "roll_number": 1, "admission_number": 1,
    }).to_list(length=2000)


async def collect_offline(
    tenant: TenantContext,
    auth: AuthContext,
    assignment_id: str,
    *,
    received: list[str],
) -> dict[str, Any]:
    """Record who handed the work in on paper.

    Written like the attendance register: the screen sends the whole roster's
    answer, not a diff, so unticking someone actually un-collects them. A row
    already graded is left alone — a mark is a statement about work that was
    seen, and a stray tick should not quietly erase it.
    """
    assignment = await collection(C.ASSIGNMENTS).find_one({
        "_id": ObjectId(assignment_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True},
    })
    if assignment is None:
        raise NotFound("Assignment not found")
    if assignment.get("submission_mode", "online") != "offline":
        raise Conflict(
            "This assignment is handed in through the portal — there is nothing to collect"
        )
    if assignment.get("status") == "draft":
        raise Conflict("Publish the assignment before collecting it")

    roster = {str(s["_id"]) for s in await expected_students(tenant, assignment)}
    ticked = {sid for sid in received if sid in roster}
    stranger = set(received) - roster
    if stranger:
        raise ValidationError("Someone on that list is not in this class")

    submissions = Repository(C.SUBMISSIONS, tenant.id, actor_id=auth.user_id)
    existing = {
        str(row["student_id"]): row
        for row in await collection(C.SUBMISSIONS).find({
            "tenant_id": tenant.id, "assignment_id": assignment["_id"],
            "is_deleted": {"$ne": True},
        }).to_list(length=2000)
    }

    due = assignment.get("due_date")
    late = bool(due and utcnow() > due.replace(tzinfo=UTC))
    added = removed = 0

    for student_id in roster:
        row = existing.get(student_id)
        if student_id in ticked:
            if row and row.get("status") == "graded":
                continue
            payload = {
                "assignment_id": assignment["_id"],
                "student_id": ObjectId(student_id),
                "submitted_at": row.get("submitted_at") if row else utcnow(),
                "status": "late" if late and not row else (row or {}).get("status") or "submitted",
                "collected_offline": True,
                "collected_by": auth.user_id,
            }
            if row:
                await submissions.update(str(row["_id"]), payload)
            else:
                payload["submitted_at"] = utcnow()
                payload["status"] = "late" if late else "submitted"
                await submissions.create(payload)
                added += 1
        elif row and row.get("collected_offline") and row.get("status") != "graded":
            # Only ever undo a tick this screen itself made.
            await submissions.delete(str(row["_id"]))
            removed += 1

    counted = await collection(C.SUBMISSIONS).count_documents({
        "tenant_id": tenant.id, "assignment_id": assignment["_id"],
        "is_deleted": {"$ne": True},
    })
    await collection(C.ASSIGNMENTS).update_one(
        {"_id": assignment["_id"]}, {"$set": {"submission_count": counted}}
    )

    return {
        "detail": f"{len(ticked)} of {len(roster)} recorded as handed in",
        "received": len(ticked),
        "expected": len(roster),
        "added": added,
        "removed": removed,
    }


async def publish_assignment(
    tenant: TenantContext, auth: AuthContext, assignment_id: str
) -> dict[str, Any]:
    """Publish, and tell the class.

    Publishing is what makes the work visible to students, so it is also the
    only honest moment to notify them — a draft that quietly became published
    at some point in the past is not news anybody can act on.
    """
    assignments = Repository(C.ASSIGNMENTS, tenant.id, actor_id=auth.user_id)
    assignment = await assignments.get_or_404(assignment_id, label="Assignment")
    if assignment.get("status") == "published":
        raise Conflict("This is already published")

    updated = await assignments.update(assignment_id, {
        "status": "published", "published_at": utcnow(),
    })

    from app.modules.communication.notify import notify_audience

    offline = assignment.get("submission_mode", "online") == "offline"
    due = assignment.get("due_date")
    await notify_audience(
        tenant,
        audience=(
            {"section_ids": [str(s) for s in assignment.get("section_ids") or []]}
            if assignment.get("section_ids")
            else {"class_ids": [str(assignment.get("class_id"))]}
        ),
        title=f"New {assignment.get('type', 'homework')}: {assignment.get('title', '')}",
        body=(
            (f"Due {due:%d %b}. " if due else "")
            + ("Hand it to your teacher in class." if offline else "Hand it in from the portal.")
        ),
        category="assignment",
        link="/portal/homework",
    )
    return {**(serialize_doc(updated) or {}), "detail": "Published and the class notified"}


async def close_assignment(
    tenant: TenantContext, auth: AuthContext, assignment_id: str
) -> dict[str, Any]:
    """Stop accepting it. Nobody is notified — a deadline passing is not news."""
    assignments = Repository(C.ASSIGNMENTS, tenant.id, actor_id=auth.user_id)
    await assignments.get_or_404(assignment_id, label="Assignment")
    updated = await assignments.update(assignment_id, {"status": "closed"})
    return {**(serialize_doc(updated) or {}), "detail": "Closed"}


async def grade_submission(
    tenant: TenantContext,
    auth: AuthContext,
    submission_id: str,
    *,
    marks: float | None = None,
    grade: str = "",
    feedback: str = "",
) -> dict[str, Any]:
    submissions = Repository(C.SUBMISSIONS, tenant.id, actor_id=auth.user_id)
    submission = await submissions.get_or_404(submission_id, label="Submission")

    assignment = await collection(C.ASSIGNMENTS).find_one({"_id": submission["assignment_id"]})
    maximum = float((assignment or {}).get("max_marks") or 0)
    if marks is not None and maximum and marks > maximum:
        raise ValidationError(f"Marks cannot exceed {maximum:g}")

    was_graded = submission.get("status") == "graded"
    updated = await submissions.update(submission_id, {
        "marks": marks,
        "grade": grade,
        "feedback": feedback,
        "status": "graded",
        "graded_by": auth.user_id,
        "graded_at": utcnow(),
    })
    if not was_graded and assignment:
        await collection(C.ASSIGNMENTS).update_one(
            {"_id": assignment["_id"]}, {"$inc": {"graded_count": 1}}
        )

    from app.modules.communication.notify import notify_users

    user_ids = await collection(C.USERS).distinct("_id", {
        "tenant_id": tenant.id, "student_id": submission["student_id"], "is_active": True,
    })
    if user_ids:
        await notify_users(
            tenant, user_ids,
            title=f"Graded: {(assignment or {}).get('title', 'your assignment')}",
            body=(f"{marks:g}/{maximum:g}" if marks is not None and maximum else grade or "See feedback"),
            category="assignment", type="success", link="/assignments",
        )

    return {**(serialize_doc(updated) or {}), "detail": "Graded"}


async def assignment_submissions(
    tenant: TenantContext, assignment_id: str
) -> dict[str, Any]:
    assignment = await collection(C.ASSIGNMENTS).find_one({
        "_id": ObjectId(assignment_id), "tenant_id": tenant.id,
    })
    if assignment is None:
        raise NotFound("Assignment not found")

    submissions = await collection(C.SUBMISSIONS).find({
        "tenant_id": tenant.id, "assignment_id": assignment["_id"],
        "is_deleted": {"$ne": True},
    }).to_list(length=2000)

    # Everyone who *should* submit, so the gaps are visible rather than implied.
    # Work set for a whole class names no sections, and reading sections alone
    # showed the teacher an empty roster for exactly those.
    expected = await expected_students(tenant, assignment)

    by_student = {s["student_id"]: s for s in submissions}
    rows = []
    for student in sorted(expected, key=lambda s: str(s.get("roll_number") or "")):
        submission = by_student.get(student["_id"])
        rows.append({
            "student_id": str(student["_id"]),
            "name": " ".join(filter(None, [student.get("first_name"),
                                           student.get("last_name")])),
            "roll_number": student.get("roll_number", ""),
            "admission_number": student.get("admission_number", ""),
            "submission_id": str(submission["_id"]) if submission else None,
            "status": submission.get("status") if submission else "pending",
            "submitted_at": (
                submission["submitted_at"].isoformat()
                if submission and submission.get("submitted_at") else None
            ),
            "marks": submission.get("marks") if submission else None,
            "grade": submission.get("grade", "") if submission else "",
            "feedback": submission.get("feedback", "") if submission else "",
            "text_answer": submission.get("text_answer", "") if submission else "",
            "attachments": submission.get("attachments", []) if submission else [],
            "collected_offline": bool(submission.get("collected_offline")) if submission else False,
        })

    return {
        "assignment": serialize_doc(assignment),
        "submission_mode": assignment.get("submission_mode", "online"),
        "expected": len(expected),
        "submitted": sum(1 for r in rows if r["status"] != "pending"),
        "graded": sum(1 for r in rows if r["status"] == "graded"),
        "late": sum(1 for r in rows if r["status"] == "late"),
        "pending": sum(1 for r in rows if r["status"] == "pending"),
        "rows": rows,
    }


async def my_assignments(tenant: TenantContext, auth: AuthContext) -> list[dict[str, Any]]:
    """Homework for the signed-in family, with each child's submission state.

    A parent holds the same view as their child — otherwise the menu shows them
    a Homework screen that is permanently empty, which is worse than no screen.
    """
    from app.core.scoping import family_student_ids

    student_ids = await family_student_ids(auth, tenant)
    if not student_ids:
        return []

    out: list[dict[str, Any]] = []
    many = len(student_ids) > 1
    for student_id in student_ids:
        out.extend(await _assignments_for_student(tenant, student_id, name_them=many))
    out.sort(key=lambda row: (row.get("due_date") or "", row.get("title", "")))
    return out


async def _assignments_for_student(
    tenant: TenantContext, student_id: ObjectId, *, name_them: bool
) -> list[dict[str, Any]]:
    student = await collection(C.STUDENTS).find_one(
        {"_id": student_id, "tenant_id": tenant.id}
    )
    section_id = (student or {}).get("current_section_id")
    class_id = (student or {}).get("current_class_id")
    if section_id is None and class_id is None:
        return []

    # A teacher who sets work for the whole class rarely lists its sections, so
    # matching on sections alone left those students with an empty screen.
    reach: list[dict[str, Any]] = []
    if section_id is not None:
        reach.append({"section_ids": section_id})
    if class_id is not None:
        reach.append({"class_id": class_id, "section_ids": {"$in": [[], None]}})
        reach.append({"class_id": class_id, "section_ids": {"$exists": False}})

    assignments = await collection(C.ASSIGNMENTS).find({
        "tenant_id": tenant.id, "is_deleted": {"$ne": True},
        "status": "published", "$or": reach,
    }).sort([("due_date", 1)]).to_list(length=200)

    submissions = {
        s["assignment_id"]: s
        for s in await collection(C.SUBMISSIONS).find({
            "tenant_id": tenant.id, "student_id": student_id,
        }).to_list(length=500)
    }
    subjects = {
        s["_id"]: s.get("name", "")
        for s in await collection(C.SUBJECTS).find({"tenant_id": tenant.id}).to_list(None)
    }

    out = []
    for assignment in assignments:
        submission = submissions.get(assignment["_id"])
        due = assignment.get("due_date")
        row = serialize_doc(assignment) or {}
        row["subject_name"] = subjects.get(assignment.get("subject_id"), "")
        row["my_status"] = submission.get("status") if submission else "pending"
        row["my_marks"] = submission.get("marks") if submission else None
        row["my_feedback"] = submission.get("feedback", "") if submission else ""
        row["submission_id"] = str(submission["_id"]) if submission else None
        # A student cannot hand in work the teacher collects on paper, so the
        # screen has to say what to do instead of offering a button that 409s.
        row["submission_mode"] = assignment.get("submission_mode", "online")
        row["collected_offline"] = bool(submission.get("collected_offline")) if submission else False
        row["is_overdue"] = bool(
            due and utcnow() > due.replace(tzinfo=UTC) and not submission
        )
        row["student_id"] = str(student_id)
        # Only worth saying whose it is when a parent is looking at several.
        row["student_name"] = (
            " ".join(filter(None, [(student or {}).get("first_name"),
                                   (student or {}).get("last_name")]))
            if name_them else ""
        )
        out.append(row)
    return out
