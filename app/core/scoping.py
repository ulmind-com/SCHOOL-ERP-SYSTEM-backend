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
