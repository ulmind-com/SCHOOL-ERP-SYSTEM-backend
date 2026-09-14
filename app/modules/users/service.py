"""Institution-side user and role administration."""

from __future__ import annotations

from typing import Any

from bson import ObjectId

from app.core.config import settings
from app.core.context import AuthContext, TenantContext
from app.core.exceptions import Conflict, Forbidden, LimitExceeded, NotFound, ValidationError
from app.core.permissions import ALL_PERMISSIONS, expand
from app.core.security import hash_password
from app.db.mongo import C, collection
from app.models.base import serialize_doc, utcnow
from app.modules.auth.service import create_invite_token, temporary_password


async def role_by_key(tenant_id: ObjectId, key: str) -> dict[str, Any] | None:
    return await collection(C.ROLES).find_one(
        {"tenant_id": tenant_id, "key": key, "is_deleted": {"$ne": True}}
    )


async def enforce_admin_seats(tenant: TenantContext) -> None:
    """Plan ceiling on staff-facing logins. Portal accounts for students and
    parents are not seats — charging a school per parent would be absurd."""
    if tenant.is_dedicated:
        return
    ceiling = tenant.limits.max_admin_users
    if ceiling is None:
        return
    portal_roles = await collection(C.ROLES).distinct(
        "_id", {"tenant_id": tenant.id, "portal": {"$in": ["student", "parent"]}}
    )
    used = await collection(C.USERS).count_documents(
        {"tenant_id": tenant.id, "is_active": True, "is_deleted": {"$ne": True},
         "role_ids": {"$not": {"$elemMatch": {"$in": portal_roles}}}}
    )
    if used >= ceiling:
        raise LimitExceeded(
            f"Your plan covers {ceiling} staff logins and {used} are in use. "
            "Upgrade to add more."
        )


async def create_user(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    email: str,
    full_name: str,
    role_ids: list[str],
    phone: str = "",
    password: str | None = None,
    send_invite: bool = True,
    student_id: ObjectId | None = None,
    staff_id: ObjectId | None = None,
    guardian_id: ObjectId | None = None,
) -> dict[str, Any]:
    users = collection(C.USERS)
    email = email.strip().lower()
    if await users.find_one({"tenant_id": tenant.id, "email": email,
                             "is_deleted": {"$ne": True}}):
        raise Conflict(f"{email} already has an account here")

    roles = await collection(C.ROLES).find(
        {"_id": {"$in": [ObjectId(r) for r in role_ids if ObjectId.is_valid(r)]},
         "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    ).to_list(length=None)
    if not roles:
        raise ValidationError("Choose at least one role")
    if any(r.get("is_owner") for r in roles) and not auth.is_owner:
        raise Forbidden("Only the institution owner can grant the owner role")

    if not any(r.get("portal") in {"student", "parent"} for r in roles):
        await enforce_admin_seats(tenant)

    temp = password or temporary_password()
    doc = {
        "tenant_id": tenant.id,
        "email": email,
        "full_name": full_name.strip(),
        "phone": phone,
        "password_hash": hash_password(temp),
        "role_ids": [r["_id"] for r in roles],
        "status": "invited" if send_invite else "active",
        "is_active": True,
        "must_change_password": password is None,
        "student_id": student_id,
        "staff_id": staff_id,
        "guardian_id": guardian_id,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "created_by": auth.user_id,
        "is_deleted": False,
    }
    user_id = (await users.insert_one(doc)).inserted_id
    await collection(C.ROLES).update_many(
        {"_id": {"$in": [r["_id"] for r in roles]}}, {"$inc": {"user_count": 1}}
    )

    # Link the login back onto the person record.
    for coll, pid in ((C.STUDENTS, student_id), (C.STAFF, staff_id), (C.GUARDIANS, guardian_id)):
        if pid:
            await collection(coll).update_one({"_id": pid}, {"$set": {"user_id": user_id}})

    result: dict[str, Any] = {
        "id": str(user_id),
        "email": email,
        "roles": [r.get("name") for r in roles],
        "detail": f"Account created for {email}",
    }
    if send_invite:
        token = await create_invite_token(user_id, tenant.id)
        # Emailing is the notification layer's job; in development we hand the
        # token back so the flow is testable without a mail server.
        if settings.debug:
            result["invite_token"] = token
    else:
        result["temporary_password"] = temp
    return result


async def create_user_for_person(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    email: str,
    full_name: str,
    role_key: str,
    student_id: ObjectId | None = None,
    staff_id: ObjectId | None = None,
    guardian_id: ObjectId | None = None,
) -> dict[str, Any] | None:
    """Convenience used when admitting a student or hiring staff."""
    role = await role_by_key(tenant.id, role_key)
    if role is None:
        return None
    existing = await collection(C.USERS).find_one(
        {"tenant_id": tenant.id, "email": email.strip().lower(), "is_deleted": {"$ne": True}}
    )
    if existing:
        return {"id": str(existing["_id"]), "email": existing["email"],
                "detail": "Account already existed"}
    return await create_user(
        tenant, auth, email=email, full_name=full_name, role_ids=[str(role["_id"])],
        student_id=student_id, staff_id=staff_id, guardian_id=guardian_id,
    )


async def update_user_roles(
    tenant: TenantContext, auth: AuthContext, user_id: str, role_ids: list[str]
) -> dict[str, Any]:
    users = collection(C.USERS)
    _id = ObjectId(user_id)
    user = await users.find_one({"_id": _id, "tenant_id": tenant.id,
                                 "is_deleted": {"$ne": True}})
    if user is None:
        raise NotFound("User not found")

    new_ids = [ObjectId(r) for r in role_ids if ObjectId.is_valid(r)]
    roles = await collection(C.ROLES).find(
        {"_id": {"$in": new_ids}, "tenant_id": tenant.id}
    ).to_list(length=None)
    if not roles:
        raise ValidationError("Choose at least one role")
    if any(r.get("is_owner") for r in roles) and not auth.is_owner:
        raise Forbidden("Only the institution owner can grant the owner role")

    # The last owner must stay an owner, or the institution locks itself out.
    was_owner = await _has_owner_role(tenant.id, user.get("role_ids") or [])
    will_be_owner = any(r.get("is_owner") for r in roles)
    if was_owner and not will_be_owner and await _owner_count(tenant.id) <= 1:
        raise ValidationError(
            "This is the only owner account. Promote someone else before changing it."
        )

    await collection(C.ROLES).update_many(
        {"_id": {"$in": user.get("role_ids") or []}}, {"$inc": {"user_count": -1}}
    )
    await collection(C.ROLES).update_many({"_id": {"$in": new_ids}}, {"$inc": {"user_count": 1}})
    await users.update_one({"_id": _id}, {"$set": {"role_ids": new_ids, "updated_at": utcnow()}})
    return {"id": user_id, "roles": [r.get("name") for r in roles], "detail": "Roles updated"}


async def _has_owner_role(tenant_id: ObjectId, role_ids: list[ObjectId]) -> bool:
    return await collection(C.ROLES).count_documents(
        {"_id": {"$in": role_ids}, "tenant_id": tenant_id, "is_owner": True}, limit=1
    ) > 0


async def _owner_count(tenant_id: ObjectId) -> int:
    owner_ids = await collection(C.ROLES).distinct(
        "_id", {"tenant_id": tenant_id, "is_owner": True}
    )
    return await collection(C.USERS).count_documents(
        {"tenant_id": tenant_id, "role_ids": {"$in": owner_ids}, "is_active": True,
         "is_deleted": {"$ne": True}}
    )


async def set_user_active(
    tenant: TenantContext, auth: AuthContext, user_id: str, active: bool
) -> dict[str, Any]:
    _id = ObjectId(user_id)
    if _id == auth.user_id and not active:
        raise ValidationError("You cannot deactivate your own account")
    user = await collection(C.USERS).find_one({"_id": _id, "tenant_id": tenant.id})
    if user is None:
        raise NotFound("User not found")
    if not active and await _has_owner_role(tenant.id, user.get("role_ids") or []) \
            and await _owner_count(tenant.id) <= 1:
        raise ValidationError("This is the only owner account and cannot be deactivated")
    await collection(C.USERS).update_one(
        {"_id": _id}, {"$set": {"is_active": active, "updated_at": utcnow()}}
    )
    if not active:
        await collection(C.SESSIONS).update_many(
            {"user_id": _id, "revoked_at": None}, {"$set": {"revoked_at": utcnow()}}
        )
    return {"id": user_id, "is_active": active,
            "detail": "Account " + ("reactivated" if active else "deactivated")}


async def reset_user_password(tenant: TenantContext, user_id: str) -> dict[str, Any]:
    temp = temporary_password()
    result = await collection(C.USERS).update_one(
        {"_id": ObjectId(user_id), "tenant_id": tenant.id},
        {"$set": {"password_hash": hash_password(temp), "must_change_password": True,
                  "updated_at": utcnow()},
         "$unset": {"locked_until": "", "failed_login_attempts": ""}},
    )
    if result.matched_count == 0:
        raise NotFound("User not found")
    await collection(C.SESSIONS).update_many(
        {"user_id": ObjectId(user_id), "revoked_at": None}, {"$set": {"revoked_at": utcnow()}}
    )
    return {"temporary_password": temp,
            "detail": "Password reset. Share the temporary password securely."}


async def list_users(
    tenant: TenantContext, *, page: int = 1, page_size: int = 25, search: str = "",
    role_id: str = "", status: str = "", portal: str = "",
) -> dict[str, Any]:
    query: dict[str, Any] = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    if search:
        query["$or"] = [
            {"full_name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
            {"phone": {"$regex": search, "$options": "i"}},
        ]
    if role_id and ObjectId.is_valid(role_id):
        query["role_ids"] = ObjectId(role_id)
    if status:
        query["status"] = status
    if portal:
        portal_role_ids = await collection(C.ROLES).distinct(
            "_id", {"tenant_id": tenant.id, "portal": portal}
        )
        query["role_ids"] = {"$in": portal_role_ids}

    users = collection(C.USERS)
    total = await users.count_documents(query)
    docs = await users.find(
        query, {"password_hash": 0, "invite_token_hash": 0, "reset_token_hash": 0}
    ).sort([("created_at", -1)]).skip((page - 1) * page_size).limit(page_size).to_list(
        length=page_size
    )

    role_ids = {r for d in docs for r in (d.get("role_ids") or [])}
    roles = {
        r["_id"]: r
        for r in await collection(C.ROLES).find({"_id": {"$in": list(role_ids)}}).to_list(
            length=None
        )
    }
    items = []
    for doc in docs:
        row = serialize_doc(doc) or {}
        row["roles"] = [
            {"id": str(rid), "name": roles.get(rid, {}).get("name", ""),
             "key": roles.get(rid, {}).get("key", "")}
            for rid in (doc.get("role_ids") or [])
        ]
        items.append(row)

    total_pages = max(1, -(-total // page_size))
    return {"items": items, "meta": {"page": page, "page_size": page_size, "total": total,
                                     "total_pages": total_pages, "has_next": page < total_pages,
                                     "has_prev": page > 1}}


# ── Roles ─────────────────────────────────────────────────────────────────
async def create_role(
    tenant: TenantContext, auth: AuthContext, *, name: str, description: str,
    permissions: list[str], portal: str = "admin",
) -> dict[str, Any]:
    key = "_".join(name.lower().split())[:40]
    if await role_by_key(tenant.id, key):
        raise Conflict(f"A role called '{name}' already exists")
    bad = [p for p in permissions if p not in ALL_PERMISSIONS and not p.endswith(":*") and p != "*"]
    if bad:
        raise ValidationError(f"Unknown permission(s): {', '.join(bad[:5])}")
    if "*" in permissions and not auth.is_owner:
        raise Forbidden("Only the institution owner can create a role with full access")
    doc = {
        "tenant_id": tenant.id, "key": key, "name": name.strip(), "description": description,
        "permissions": permissions, "portal": portal, "is_system": False, "is_owner": False,
        "user_count": 0, "created_at": utcnow(), "updated_at": utcnow(),
        "created_by": auth.user_id, "is_deleted": False,
    }
    role_id = (await collection(C.ROLES).insert_one(doc)).inserted_id
    return {"id": str(role_id), "key": key, "detail": f"Role '{name}' created"}


async def update_role(
    tenant: TenantContext, auth: AuthContext, role_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    _id = ObjectId(role_id)
    role = await collection(C.ROLES).find_one({"_id": _id, "tenant_id": tenant.id})
    if role is None:
        raise NotFound("Role not found")
    if role.get("is_owner"):
        raise Forbidden("The owner role's permissions cannot be edited")
    if "permissions" in data:
        if "*" in data["permissions"] and not auth.is_owner:
            raise Forbidden("Only the institution owner can grant full access")
        unknown = [
            p for p in data["permissions"]
            if p not in ALL_PERMISSIONS and not p.endswith(":*") and p != "*"
        ]
        if unknown:
            raise ValidationError(f"Unknown permission(s): {', '.join(unknown[:5])}")
    payload = {k: v for k, v in data.items()
               if k in {"name", "description", "permissions", "portal"} and v is not None}
    payload["updated_at"] = utcnow()
    doc = await collection(C.ROLES).find_one_and_update(
        {"_id": _id}, {"$set": payload}, return_document=True
    )
    return serialize_doc(doc)  # type: ignore[return-value]


async def delete_role(tenant: TenantContext, role_id: str) -> None:
    _id = ObjectId(role_id)
    role = await collection(C.ROLES).find_one({"_id": _id, "tenant_id": tenant.id})
    if role is None:
        raise NotFound("Role not found")
    if role.get("is_owner"):
        raise Forbidden("The owner role cannot be deleted")
    if role.get("is_system"):
        raise Forbidden("Built-in roles cannot be deleted — edit their permissions instead")
    in_use = await collection(C.USERS).count_documents(
        {"tenant_id": tenant.id, "role_ids": _id, "is_deleted": {"$ne": True}}
    )
    if in_use:
        raise Conflict(f"{in_use} user(s) still hold this role. Reassign them first.")
    await collection(C.ROLES).update_one(
        {"_id": _id}, {"$set": {"is_deleted": True, "updated_at": utcnow()}}
    )


async def list_roles(tenant: TenantContext) -> list[dict[str, Any]]:
    docs = await collection(C.ROLES).find(
        {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    ).sort([("is_owner", -1), ("name", 1)]).to_list(length=None)
    out = []
    for doc in docs:
        row = serialize_doc(doc) or {}
        row["effective_permission_count"] = len(expand(doc.get("permissions") or []))
        out.append(row)
    return out
