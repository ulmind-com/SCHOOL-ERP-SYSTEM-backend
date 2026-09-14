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
