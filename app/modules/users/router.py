"""Users, roles and permissions — the institution's own access control."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request

from app.core.context import AuthContext
from app.core.deps import CurrentUser, TenantDep, require
from app.core.permissions import ROLE_PRESETS, module_group_tree
from app.db.mongo import C, collection
from app.models.base import AppModel, Msg, serialize_doc
from app.modules.users import service
from app.utils.audit import record

router = APIRouter(tags=["Users & Access"])

UserReader = Annotated[AuthContext, Depends(require("users:read"))]
UserWriter = Annotated[AuthContext, Depends(require("users:create"))]
UserEditor = Annotated[AuthContext, Depends(require("users:update"))]
RoleReader = Annotated[AuthContext, Depends(require("roles:read"))]
RoleWriter = Annotated[AuthContext, Depends(require("roles:create"))]
RoleEditor = Annotated[AuthContext, Depends(require("roles:update"))]
RoleDeleter = Annotated[AuthContext, Depends(require("roles:delete"))]


class UserCreateRequest(AppModel):
    email: str
    full_name: str
    role_ids: list[str]
    phone: str = ""
    password: str | None = None
    send_invite: bool = True
    staff_id: str | None = None
    student_id: str | None = None
    guardian_id: str | None = None


class RolesRequest(AppModel):
    role_ids: list[str]


class RoleCreateRequest(AppModel):
    name: str
    description: str = ""
    permissions: list[str] = []
    portal: str = "admin"


class RoleUpdateRequest(AppModel):
    name: str | None = None
    description: str | None = None
    permissions: list[str] | None = None
    portal: str | None = None


# ── Users ─────────────────────────────────────────────────────────────────
@router.get("/users", summary="List institution users")
async def list_users(
    auth: UserReader,
    tenant: TenantDep,
    page: int = 1,
    page_size: Annotated[int, Query(le=200)] = 25,
    search: str = "",
    role_id: str = "",
    status: str = "",
    portal: str = "",
):
    return await service.list_users(
        tenant, page=page, page_size=page_size, search=search,
        role_id=role_id, status=status, portal=portal,
    )


@router.post("/users", status_code=201, summary="Invite a user")
async def create_user(
    payload: UserCreateRequest, auth: UserWriter, tenant: TenantDep, request: Request
):
    from bson import ObjectId

    result = await service.create_user(
        tenant, auth,
        email=payload.email, full_name=payload.full_name, role_ids=payload.role_ids,
        phone=payload.phone, password=payload.password, send_invite=payload.send_invite,
        staff_id=ObjectId(payload.staff_id) if payload.staff_id else None,
        student_id=ObjectId(payload.student_id) if payload.student_id else None,
        guardian_id=ObjectId(payload.guardian_id) if payload.guardian_id else None,
    )
    await record(auth, "users.create", entity_type="users", entity_id=result["id"],
                 entity_label=payload.email, request=request)
    return result


@router.get("/users/{user_id}", summary="Get a user")
async def get_user(user_id: str, auth: UserReader, tenant: TenantDep):
    from bson import ObjectId

    doc = await collection(C.USERS).find_one(
        {"_id": ObjectId(user_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}},
        {"password_hash": 0, "invite_token_hash": 0, "reset_token_hash": 0},
    )
    if doc is None:
        from app.core.exceptions import NotFound

        raise NotFound("User not found")
    row = serialize_doc(doc) or {}
    roles = await collection(C.ROLES).find(
        {"_id": {"$in": doc.get("role_ids") or []}}
    ).to_list(length=None)
    row["roles"] = [serialize_doc(r) for r in roles]
    return row


@router.put("/users/{user_id}/roles", summary="Change a user's roles")
async def set_roles(
    user_id: str, payload: RolesRequest, auth: UserEditor, tenant: TenantDep, request: Request
):
    result = await service.update_user_roles(tenant, auth, user_id, payload.role_ids)
    await record(auth, "users.roles_changed", entity_type="users", entity_id=user_id,
                 changes={"roles": result["roles"]}, request=request)
    return result


@router.post("/users/{user_id}/activate", summary="Reactivate an account")
async def activate(user_id: str, auth: UserEditor, tenant: TenantDep, request: Request):
    result = await service.set_user_active(tenant, auth, user_id, True)
    await record(auth, "users.activate", entity_type="users", entity_id=user_id, request=request)
    return result


@router.post("/users/{user_id}/deactivate", summary="Deactivate an account")
async def deactivate(user_id: str, auth: UserEditor, tenant: TenantDep, request: Request):
    result = await service.set_user_active(tenant, auth, user_id, False)
    await record(auth, "users.deactivate", entity_type="users", entity_id=user_id,
                 request=request)
    return result


@router.post("/users/{user_id}/reset-password", summary="Issue a temporary password")
async def reset_password(user_id: str, auth: UserEditor, tenant: TenantDep, request: Request):
    result = await service.reset_user_password(tenant, user_id)
    await record(auth, "users.password_reset", entity_type="users", entity_id=user_id,
                 request=request)
    return result


class ImpersonateRequest(AppModel):
    reason: str = ""


@router.post("/users/{user_id}/impersonate",
             summary="Open a short session inside another account")
async def impersonate(
    user_id: str,
    payload: ImpersonateRequest,
    auth: UserEditor,
    tenant: TenantDep,
    request: Request,
):
    """For support: see the app exactly as this person sees it.

    The session lasts thirty minutes, cannot be refreshed, and every action
    taken during it is recorded against the administrator who opened it.
    """
    result = await service.impersonation_session(tenant, auth, user_id)
    await record(auth, "users.impersonate", entity_type="users", entity_id=user_id,
                 entity_label=result["user"]["email"],
                 changes={"reason": payload.reason}, request=request)
    return result


class SendCredentialsRequest(AppModel):
    #: student | guardian | staff
    person_type: str
    person_id: str
    #: Which role the new login gets. Defaults to the obvious one for the type.
    role_key: str | None = None


@router.post("/users/send-credentials",
             summary="Create or reset a person's login and email it to them")
async def send_credentials(
    payload: SendCredentialsRequest, auth: UserEditor, tenant: TenantDep, request: Request
):
    """Creating and re-sending are one request, because from the office's side
    they are one intention: get this person in."""
    result = await service.send_credentials(
        tenant, auth,
        person_type=payload.person_type, person_id=payload.person_id,
        role_key=payload.role_key,
    )
    await record(auth, "users.credentials_sent", entity_type=payload.person_type,
                 entity_id=payload.person_id, entity_label=result["email"],
                 changes={"created": result["created"]}, request=request)
    return result


@router.post("/users/{user_id}/resend-invite", summary="Send the invitation again")
async def resend_invite(user_id: str, auth: UserEditor, tenant: TenantDep, request: Request):
    """Issues a fresh password and emails it — the old invitation's password is
    no longer valid, which is the point: an invitation nobody acted on has been
    sitting in an inbox."""
    result = await service.reset_user_password(tenant, user_id)
    await record(auth, "users.invite_resent", entity_type="users", entity_id=user_id,
                 request=request)
    return result


# ── Roles ─────────────────────────────────────────────────────────────────
@router.post("/roles/sync", summary="Bring untouched built-in roles up to date")
async def sync_roles(auth: RoleEditor, tenant: TenantDep, request: Request):
    """Roles are documents per institution, so a preset corrected in a release
    never reaches the schools already running — including when the correction
    was a permission that should not have been granted. Roles the institution
    has edited are left alone."""
    result = await service.sync_builtin_roles(tenant)
    if result["roles_updated"]:
        await record(auth, "roles.synced", entity_type="roles",
                     changes=result, request=request)
    return result


@router.get("/roles", summary="List roles")
async def list_roles(auth: RoleReader, tenant: TenantDep):
    return await service.list_roles(tenant)


@router.get("/roles/catalogue", summary="Permission catalogue and role presets")
async def catalogue(auth: RoleReader, tenant: TenantDep):
    """What the role editor renders: every module grouped, with the modules this
    institution actually has switched on flagged."""
    groups = module_group_tree()
    for group in groups:
        for module in group["modules"]:
            module["enabled"] = tenant.module_enabled(module["key"])
    return {
        "groups": groups,
        "presets": [
            {"key": p.key, "name": p.name, "description": p.description, "portal": p.portal}
            for p in ROLE_PRESETS
        ],
        "portals": ["admin", "finance", "teacher", "student", "parent"],
    }


@router.post("/roles", status_code=201, summary="Create a custom role")
async def create_role(
    payload: RoleCreateRequest, auth: RoleWriter, tenant: TenantDep, request: Request
):
    result = await service.create_role(
        tenant, auth, name=payload.name, description=payload.description,
        permissions=payload.permissions, portal=payload.portal,
    )
    await record(auth, "roles.create", entity_type="roles", entity_id=result["id"],
                 entity_label=payload.name, request=request)
    return result


@router.patch("/roles/{role_id}", summary="Edit a role")
async def update_role(
    role_id: str, payload: RoleUpdateRequest, auth: RoleEditor, tenant: TenantDep,
    request: Request,
):
    result = await service.update_role(tenant, auth, role_id, payload.model_dump(exclude_none=True))
    await record(auth, "roles.update", entity_type="roles", entity_id=role_id,
                 entity_label=result.get("name", ""), request=request)
    return result


@router.delete("/roles/{role_id}", response_model=Msg, summary="Delete a custom role")
async def delete_role(role_id: str, auth: RoleDeleter, tenant: TenantDep, request: Request):
    await service.delete_role(tenant, role_id)
    await record(auth, "roles.delete", entity_type="roles", entity_id=role_id, request=request)
    return Msg(detail="Role deleted")


# ── Audit ─────────────────────────────────────────────────────────────────
@router.get("/audit-log", summary="Institution activity trail")
async def audit_log(
    auth: Annotated[AuthContext, Depends(require("audit:read"))],
    tenant: TenantDep,
    page: int = 1,
    page_size: Annotated[int, Query(le=200)] = 50,
    action: str = "",
    entity_type: str = "",
    actor_id: str = "",
):
    from bson import ObjectId

    query: dict = {"tenant_id": tenant.id}
    if action:
        query["action"] = {"$regex": f"^{action}", "$options": "i"}
    if entity_type:
        query["entity_type"] = entity_type
    if actor_id and ObjectId.is_valid(actor_id):
        query["actor_id"] = ObjectId(actor_id)

    logs = collection(C.AUDIT)
    total = await logs.count_documents(query)
    docs = await logs.find(query).sort([("created_at", -1)]).skip(
        (page - 1) * page_size
    ).limit(page_size).to_list(length=page_size)
    total_pages = max(1, -(-total // page_size))
    return {
        "items": [serialize_doc(d) for d in docs],
        "meta": {"page": page, "page_size": page_size, "total": total,
                 "total_pages": total_pages, "has_next": page < total_pages,
                 "has_prev": page > 1},
    }


# ── Notifications (own) ───────────────────────────────────────────────────
@router.get("/notifications", summary="Your notifications")
async def notifications(auth: CurrentUser, tenant: TenantDep, unread_only: bool = False):
    query: dict = {"tenant_id": tenant.id, "user_id": auth.user_id}
    if unread_only:
        query["read_at"] = None
    docs = await collection(C.NOTIFICATIONS).find(query).sort(
        [("created_at", -1)]
    ).limit(50).to_list(length=50)
    unread = await collection(C.NOTIFICATIONS).count_documents(
        {"tenant_id": tenant.id, "user_id": auth.user_id, "read_at": None}
    )
    return {"items": [serialize_doc(d) for d in docs], "unread": unread}


@router.post("/notifications/read", response_model=Msg, summary="Mark notifications read")
async def mark_read(
    auth: CurrentUser, tenant: TenantDep, ids: Annotated[list[str] | None, Body(embed=True)] = None
):
    from bson import ObjectId

    from app.models.base import utcnow

    query: dict = {"tenant_id": tenant.id, "user_id": auth.user_id, "read_at": None}
    if ids:
        query["_id"] = {"$in": [ObjectId(i) for i in ids if ObjectId.is_valid(i)]}
    result = await collection(C.NOTIFICATIONS).update_many(
        query, {"$set": {"read_at": utcnow()}}
    )
    return Msg(detail=f"{result.modified_count} marked read")
