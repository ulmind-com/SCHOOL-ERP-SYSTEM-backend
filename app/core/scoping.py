"""Row-level scoping for the portals.

Permissions answer "may this user read invoices?"; they cannot answer "*whose*
invoices?". A student and a parent both legitimately hold ``invoices:read`` —
what makes the portal safe is that the query is narrowed to their own family
before it reaches Mongo.

These hooks are attached to resource descriptions, so the narrowing happens
inside the CRUD factory rather than in each route. A route that forgets one
gets no data rather than someone else's.
"""

from typing import Any

from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.db.mongo import C, collection

#: Portals whose users may only ever see their own family's records.
FAMILY_PORTALS = {"student", "parent"}


async def family_student_ids(
    auth: AuthContext, tenant: TenantContext
) -> list[ObjectId] | None:
    """The students this user may see, or ``None`` when unrestricted.

    An empty list is not the same as ``None``: a guardian with no children
    linked must see nothing, not everything.
    """
    if auth.student_id:
        return [auth.student_id]
    if auth.guardian_id:
        guardian = await collection(C.GUARDIANS).find_one(
            {"_id": auth.guardian_id, "tenant_id": tenant.id}
        )
        return list((guardian or {}).get("student_ids", []))
    if auth.portal in FAMILY_PORTALS:
        # A portal account with neither link attached owns no records at all.
        return []
    return None


async def student_row_scope(auth: AuthContext, tenant: TenantContext) -> dict[str, Any]:
    """Narrow a collection whose rows carry ``student_id``."""
    allowed = await family_student_ids(auth, tenant)
    return {} if allowed is None else {"student_id": {"$in": allowed}}


async def own_student_scope(auth: AuthContext, tenant: TenantContext) -> dict[str, Any]:
    """Narrow the students collection itself, where the key is ``_id``."""
    allowed = await family_student_ids(auth, tenant)
    return {} if allowed is None else {"_id": {"$in": allowed}}


#: Staff who are narrowed to what they actually teach.
TEACHING_PORTALS = {"teacher"}


async def teacher_section_ids(
    auth: AuthContext, tenant: TenantContext
) -> list[ObjectId] | None:
    """The sections this teacher takes, this academic year — or ``None`` for
    anyone who is not a class teacher at all.

    Two ways in, because a school has two: the subject they are timetabled for,
    and the section they are class teacher of. Both are recorded against an
    academic year, which is the whole point — a teacher who takes Class 8 and
    Class 11 this year has no business in Class 7's register, and next year,
    when the allocation changes, the reach changes with it rather than
    accumulating.
    """
    if auth.portal not in TEACHING_PORTALS or not auth.staff_id:
        return None

    year_id = tenant.year_for(auth)
    year_filter: dict[str, Any] = {"academic_year_id": year_id} if year_id else {}

    taught = await collection(C.SUBJECT_ASSIGNMENTS).distinct("section_id", {
        "tenant_id": tenant.id, "staff_id": auth.staff_id,
        "is_deleted": {"$ne": True}, **year_filter,
    })
    owned = await collection(C.SECTIONS).distinct("_id", {
        "tenant_id": tenant.id, "class_teacher_id": auth.staff_id,
        "is_deleted": {"$ne": True}, **year_filter,
    })
    return list({*taught, *owned})


async def teacher_section_scope(
    auth: AuthContext, tenant: TenantContext
) -> dict[str, Any]:
    """Narrow a collection that names sections in ``section_ids``."""
    sections = await teacher_section_ids(auth, tenant)
    if sections is None:
        return {}
    return {"section_ids": {"$in": sections}}


async def teacher_own_sections_scope(
    auth: AuthContext, tenant: TenantContext
) -> dict[str, Any]:
    """Narrow the sections collection itself, where the key is ``_id``.

    So the class picker on every screen offers a teacher the three sections
    they take rather than the school's twelve.
    """
    sections = await teacher_section_ids(auth, tenant)
    if sections is None:
        return {}
    return {"_id": {"$in": sections}}


async def teacher_student_scope(
    auth: AuthContext, tenant: TenantContext
) -> dict[str, Any]:
    """Narrow the students collection to the ones this teacher actually takes."""
    sections = await teacher_section_ids(auth, tenant)
    if sections is None:
        return {}
    return {"current_section_id": {"$in": sections}}


async def academic_year_scope(auth: AuthContext, tenant: TenantContext) -> dict[str, Any]:
    """Show one academic year at a time.

    Staff read whichever year they have switched to and the current one by
    default; a family always reads the current one, because a child is in one
    year at a time. Rows that name no year stay visible in every year — a row
    that does not say which year it belongs to belongs to all of them, and
    hiding it would make a switch look like data loss.
    """
    year_id = tenant.year_for(auth)
    if year_id is None:
        return {}
    return {"academic_year_id": {"$in": [year_id, None]}}


def all_of(*hooks: Any) -> Any:
    """Every hook has to hold.

    Merged under ``$and`` rather than by updating one dict into another: two
    hooks may each need ``$or`` — a class-or-section rule and a this-year-or-
    undated rule both do — and the second would otherwise erase the first.
    """

    async def _combined(auth: AuthContext, tenant: TenantContext) -> dict[str, Any]:
        parts = [part for hook in hooks if (part := await hook(auth, tenant))]
        if not parts:
            return {}
        return parts[0] if len(parts) == 1 else {"$and": parts}

    return _combined


async def family_assignment_scope(
    auth: AuthContext, tenant: TenantContext
) -> dict[str, Any]:
    """Narrow assignments to the family's own class, and to published work.

    Stricter than :func:`family_class_scope` in two ways that matter. Work set
    for a section is only theirs if it is *their* section — a class-wide row
    names no sections and reaches everyone in the class. And a draft is a
    teacher still writing: nobody outside the staffroom should see it, least of
    all a parent who then asks about homework that was never set.
    """
    allowed = await family_student_ids(auth, tenant)
    if allowed is None:
        return {}
    if not allowed:
        return {"_id": {"$in": []}}

    students = await collection(C.STUDENTS).find(
        {"_id": {"$in": allowed}, "tenant_id": tenant.id},
        projection={"current_class_id": 1, "current_section_id": 1},
    ).to_list(length=None)
    class_ids = [s["current_class_id"] for s in students if s.get("current_class_id")]
    section_ids = [s["current_section_id"] for s in students if s.get("current_section_id")]

    return {
        "status": "published",
        "$or": [
            {"section_ids": {"$in": section_ids}},
            {"class_id": {"$in": class_ids}, "section_ids": {"$in": [[], None]}},
            {"class_id": {"$in": class_ids}, "section_ids": {"$exists": False}},
        ],
    }


async def assignment_reach_scope(
    auth: AuthContext, tenant: TenantContext
) -> dict[str, Any]:
    """Who an assignment is visible to, from either end.

    A family sees their own child's published work. A teacher sees the sections
    they take this year plus anything they set themselves — the second clause
    matters because a teacher who sets work and is then moved off the section
    should still be able to finish marking it. Everyone else in the office sees
    the institution's.
    """
    family = await family_assignment_scope(auth, tenant)
    if family:
        return family

    sections = await teacher_section_ids(auth, tenant)
    if sections is None:
        return {}
    if not sections:
        # Timetabled for nothing this year: their own work, and no one else's.
        return {"assigned_by": auth.staff_id}

    class_ids = await collection(C.SECTIONS).distinct(
        "class_id", {"_id": {"$in": sections}, "tenant_id": tenant.id}
    )
    return {
        "$or": [
            {"assigned_by": auth.staff_id},
            {"section_ids": {"$in": sections}},
            # Work set for a whole class names no sections at all.
            {"class_id": {"$in": class_ids}, "section_ids": {"$in": [[], None]}},
            {"class_id": {"$in": class_ids}, "section_ids": {"$exists": False}},
        ]
    }


async def assert_may_open_section(
    auth: AuthContext, tenant: TenantContext, section_id: str | ObjectId
) -> ObjectId:
    """Guard a route that takes a section in the path.

    Narrowing a list is not enough when the next screen along takes an id: a
    teacher who is not shown Class 7 should also not be able to open its
    register by typing the id in.
    """
    from app.core.exceptions import Forbidden

    oid_value = ObjectId(section_id) if not isinstance(section_id, ObjectId) else section_id
    allowed = await teacher_section_ids(auth, tenant)
    if allowed is not None and oid_value not in allowed:
        raise Forbidden("You do not take that section this academic year")
    return oid_value


async def assert_not_family(auth: AuthContext) -> None:
    """Refuse a route that is about a class rather than about one child.

    ``assignments:read`` is what lets a parent see their own child's homework;
    it is not a reason to hand them the roster — every classmate's name, whether
    they handed it in, and what they scored.
    """
    from app.core.exceptions import Forbidden

    if auth.portal in FAMILY_PORTALS or auth.student_id or auth.guardian_id:
        raise Forbidden("Your own homework is on the Homework screen")


async def assert_may_see_student(
    auth: AuthContext, tenant: TenantContext, student_id: str | ObjectId
) -> ObjectId:
    """Guard for hand-written routes that take a student id in the path."""
    from app.core.exceptions import Forbidden

    oid = ObjectId(student_id) if not isinstance(student_id, ObjectId) else student_id
    allowed = await family_student_ids(auth, tenant)
    if allowed is not None and oid not in allowed:
        raise Forbidden("You can only view your own records")
    return oid


async def family_class_scope(auth: AuthContext, tenant: TenantContext) -> dict[str, Any]:
    """Narrow a collection keyed by ``class_id`` to the family's own classes.

    A student looking at Subjects should see the twelve their school teaches
    across two years as the six that are theirs. Rows with no class at all stay
    visible — an institution-wide subject belongs to everyone.
    """
    allowed = await family_student_ids(auth, tenant)
    if allowed is None:
        return {}
    if not allowed:
        return {"class_id": {"$in": []}}
    class_ids = [
        student["current_class_id"]
        for student in await collection(C.STUDENTS).find(
            {"_id": {"$in": allowed}, "tenant_id": tenant.id},
            projection={"current_class_id": 1},
        ).to_list(length=None)
        if student.get("current_class_id")
    ]
    return {"class_id": {"$in": [*class_ids, None]}}
