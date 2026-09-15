"""FastAPI dependencies — the single gate every protected route passes through.

The order is deliberate: identify the institution, identify the user, confirm
the institution is allowed to use the module, then confirm the user is allowed
to perform the action.
"""

from __future__ import annotations

import time
from typing import Annotated

from bson import ObjectId
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.context import AuthContext, TenantContext
from app.core.exceptions import (
    Forbidden,
    ModuleDisabled,
    SubscriptionError,
    Unauthorized,
)
from app.core.permissions import PLATFORM_PERMISSIONS, expand, has_permission
from app.core.security import decode_token
from app.core.tenancy import resolve_tenant
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models.base import oid

bearer = HTTPBearer(auto_error=False, description="Bearer access token")

BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
TenantHeader = Annotated[str | None, Header(alias="X-Tenant")]
YearHeader = Annotated[str | None, Header(alias="X-Academic-Year")]


# ── Tenant ────────────────────────────────────────────────────────────────
async def get_tenant(
    request: Request,
    credentials: BearerDep = None,
    x_tenant: TenantHeader = None,
    x_academic_year: YearHeader = None,
) -> TenantContext:
    payload = decode_token(credentials.credentials) if credentials else None
    tenant = await resolve_tenant(
        token_tenant_id=(payload or {}).get("tid"),
        header_tenant=x_tenant,
        host=request.headers.get("host"),
    )
    assert tenant is not None  # resolve_tenant raises when required and missing
    await apply_academic_year(tenant, x_academic_year)
    return tenant


async def get_optional_tenant(
    request: Request,
    credentials: BearerDep = None,
    x_tenant: TenantHeader = None,
    x_academic_year: YearHeader = None,
) -> TenantContext | None:
    payload = decode_token(credentials.credentials) if credentials else None
    tenant = await resolve_tenant(
        token_tenant_id=(payload or {}).get("tid"),
        header_tenant=x_tenant,
        host=request.headers.get("host"),
        required=False,
    )
    # /auth/me resolves the tenant through this one, and it is what tells the
    # client which year it is reading — leaving the header out here meant the
    # data switched and the label did not.
    if tenant is not None:
        await apply_academic_year(tenant, x_academic_year)
    return tenant


async def apply_academic_year(tenant: TenantContext, requested: str | None) -> None:
    """Honour an ``X-Academic-Year`` header, if it names a real year here.

    Validated against the institution's own years rather than trusted, so the
    header can only ever move the reader between that school's years — an
    unknown id silently leaves them on the current one rather than showing them
    an empty school and no reason why.

    Who may actually use it is :meth:`TenantContext.year_for`'s business; this
    only resolves it.
    """
    if not requested:
        return
    year_id = oid(requested)
    if year_id is None or year_id == tenant.current_academic_year_id:
        return
    if year_id in await academic_year_ids(tenant.id):
        tenant.active_academic_year_id = year_id


_YEAR_CACHE: dict[str, tuple[float, set[ObjectId]]] = {}
_YEAR_CACHE_TTL = 60.0


async def academic_year_ids(tenant_id: ObjectId) -> set[ObjectId]:
    """Every academic year this institution has, cached — it changes once a year."""
    key = str(tenant_id)
    hit = _YEAR_CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    ids = set(
        await collection(C.ACADEMIC_YEARS).distinct(
            "_id", {"tenant_id": tenant_id, "is_deleted": {"$ne": True}}
        )
    )
    _YEAR_CACHE[key] = (time.monotonic() + _YEAR_CACHE_TTL, ids)
    return ids


TenantDep = Annotated[TenantContext, Depends(get_tenant)]
OptionalTenantDep = Annotated[TenantContext | None, Depends(get_optional_tenant)]


# ── Authentication ────────────────────────────────────────────────────────
async def _permissions_for(tenant_id: ObjectId, role_ids: list[ObjectId]) -> tuple[set[str], list[str], list[str], bool, str]:
    if not role_ids:
        return set(), [], [], False, "admin"
    roles = await collection(C.ROLES).find(
        {"_id": {"$in": role_ids}, "tenant_id": tenant_id, "is_deleted": {"$ne": True}}
    ).to_list(length=None)
    granted: list[str] = []
    keys: list[str] = []
    names: list[str] = []
    is_owner = False
    portal = "admin"
    for role in roles:
        granted.extend(role.get("permissions") or [])
        keys.append(role.get("key", ""))
        names.append(role.get("name", ""))
        is_owner = is_owner or bool(role.get("is_owner"))
        portal = role.get("portal") or portal
    # A staff-facing portal wins over a self-service one for multi-role users.
    priority = ["platform", "admin", "finance", "teacher", "parent", "student"]
    portals = [r.get("portal", "admin") for r in roles] or ["admin"]
    portal = min(portals, key=lambda p: priority.index(p) if p in priority else 99)
    return expand(granted) | set(granted), keys, names, is_owner, portal


async def get_current_auth(
    request: Request,
    credentials: BearerDep = None,
    x_tenant: TenantHeader = None,
) -> AuthContext:
    if credentials is None:
        raise Unauthorized("Sign in to continue")
    payload = decode_token(credentials.credentials, expected_type="access")
    if payload is None:
        raise Unauthorized("Your session has expired. Sign in again.")

    user_id = oid(payload.get("sub"))
    if user_id is None:
        raise Unauthorized("Invalid session")

    # ── Platform staff (SaaS deployments only) ────────────────────────────
    if payload.get("scope") == "platform":
        if not settings.is_saas:
            raise Forbidden("The platform console is not available on this deployment")
        user = await collection(C.PLATFORM_USERS).find_one(
            {"_id": user_id, "is_active": True, "is_deleted": {"$ne": True}}
        )
        if user is None:
            raise Unauthorized("Account is no longer active")
        role = user.get("role", "platform_support")
        return AuthContext(
            user_id=user_id,
            email=user.get("email", ""),
            full_name=user.get("full_name", ""),
            scope="platform",
            role_keys=[role],
            role_names=[role.replace("_", " ").title()],
            permissions=set(PLATFORM_PERMISSIONS.get(role, [])),
            portal="platform",
            is_owner=role == "platform_owner",
            avatar_url=user.get("avatar_url", ""),
        )

    # ── Institution user ──────────────────────────────────────────────────
    tenant_id = oid(payload.get("tid"))
    if tenant_id is None:
        raise Unauthorized("Session is not bound to an institution")

    user = await collection(C.USERS).find_one(
        {"_id": user_id, "tenant_id": tenant_id, "is_deleted": {"$ne": True}}
    )
    if user is None:
        raise Unauthorized("Account not found")
    if not user.get("is_active", True):
        raise Forbidden("This account has been deactivated. Contact your administrator.")

    role_ids = [r for r in (user.get("role_ids") or []) if isinstance(r, ObjectId)]
    permissions, keys, names, is_owner, portal = await _permissions_for(tenant_id, role_ids)

    return AuthContext(
        user_id=user_id,
        email=user.get("email", ""),
        full_name=user.get("full_name", ""),
        scope="tenant",
        tenant_id=tenant_id,
        role_keys=keys,
        role_names=names,
        permissions=permissions,
        portal=portal,
        is_owner=is_owner,
        student_id=user.get("student_id"),
        staff_id=user.get("staff_id"),
        guardian_id=user.get("guardian_id"),
        impersonating=bool(payload.get("imp")),
        impersonated_by_id=oid(payload.get("act")),
        impersonated_by_name=str(payload.get("actn") or ""),
        avatar_url=user.get("avatar_url", ""),
    )


CurrentUser = Annotated[AuthContext, Depends(get_current_auth)]


async def get_current_tenant_user(auth: CurrentUser) -> AuthContext:
    if auth.is_platform:
        raise Forbidden("This endpoint is for institution users")
    return auth


TenantUser = Annotated[AuthContext, Depends(get_current_tenant_user)]


# ── Authorisation ─────────────────────────────────────────────────────────
def require(*permissions: str, mode: str = "any"):
    """Route guard: caller must hold the permission(s).

    Also enforces that the institution's subscription is live and the module is
    actually switched on for them — both no-ops on a dedicated deployment.
    """

    check_all = mode == "all"

    async def _guard(auth: CurrentUser, tenant: TenantDep) -> AuthContext:
        if not tenant.is_usable:
            raise SubscriptionError(
                f"This institution's account is {tenant.status}. Contact support to restore access."
            )
        if settings.is_saas and tenant.subscription_status in {"expired", "cancelled"}:
            raise SubscriptionError("The subscription has ended. Renew to continue.")

        for permission in permissions:
            module = permission.split(":", 1)[0]
            if not tenant.module_enabled(module):
                raise ModuleDisabled(
                    f"The {module.replace('_', ' ')} module is not enabled for your institution."
                )

        ok = (
            all(has_permission(auth.permissions, p) for p in permissions)
            if check_all
            else any(has_permission(auth.permissions, p) for p in permissions)
        )
        if not ok:
            raise Forbidden(
                "You do not have permission to do that. Ask an administrator for access."
            )
        return auth

    return _guard


def require_platform(*permissions: str):
    """Guard for the company-side console. Never mounted on dedicated builds."""

    async def _guard(auth: CurrentUser) -> AuthContext:
        if not auth.is_platform:
            raise Forbidden("Platform access required")
        if permissions and not any(has_permission(auth.permissions, p) for p in permissions):
            raise Forbidden("Your platform role does not allow that")
        return auth

    return _guard


def require_module(key: str):
    async def _guard(tenant: TenantDep) -> TenantContext:
        if not tenant.module_enabled(key):
            raise ModuleDisabled(f"The {key.replace('_', ' ')} module is not enabled.")
        return tenant

    return _guard


def require_owner(auth: CurrentUser) -> AuthContext:
    if not auth.is_owner:
        raise Forbidden("Only the institution owner can do that")
    return auth


# ── Repository factory ────────────────────────────────────────────────────
def repo_for(name: str):
    """Bind a collection to the caller's institution for the life of a request."""

    def _factory(tenant: TenantDep, auth: CurrentUser) -> Repository:
        return Repository(name, tenant.id, actor_id=auth.user_id)

    return _factory


def platform_repo(name: str):
    def _factory() -> Repository:
        return Repository(name, tenant_scoped=False)

    return _factory
