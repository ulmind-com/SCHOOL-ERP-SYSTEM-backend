"""Sign-in, sessions and password lifecycle."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from fastapi import Request

from app.core.config import settings
from app.core.context import AuthContext, TenantContext
from app.core.deps import _permissions_for
from app.core.exceptions import Conflict, Forbidden, Unauthorized, ValidationError
from app.core.navigation import build_navigation
from app.core.permissions import ALL_MODULE_KEYS, PLATFORM_PERMISSIONS
from app.core.security import (
    create_token,
    fingerprint,
    hash_password,
    password_problems,
    random_token,
    verify_password,
)
from app.core.tenancy import build_context, load_tenant_doc
from app.db.mongo import C, collection
from app.models.base import serialize_doc, utcnow
from app.modules.auth.schemas import (
    InstitutionChoice,
    SessionInstitution,
    SessionUser,
    TokenPair,
)
from app.utils.audit import client_ip, record

log = logging.getLogger(__name__)

MAX_FAILED_ATTEMPTS = 8
LOCKOUT_MINUTES = 15


# ── Helpers ───────────────────────────────────────────────────────────────
def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _issue_tokens(
    user_id: ObjectId,
    *,
    scope: str = "tenant",
    tenant_id: ObjectId | None = None,
    request: Request | None = None,
    remember: bool = True,
) -> TokenPair:
    access = create_token(str(user_id), "access", scope=scope,
                          tenant_id=str(tenant_id) if tenant_id else None)
    refresh_ttl = timedelta(days=settings.refresh_token_expire_days if remember else 1)
    refresh = create_token(str(user_id), "refresh", scope=scope,
                           tenant_id=str(tenant_id) if tenant_id else None,
                           expires_delta=refresh_ttl)
    await collection(C.SESSIONS).insert_one(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "scope": scope,
            "token_hash": fingerprint(refresh),
            "user_agent": (request.headers.get("user-agent", "")[:300] if request else ""),
            "ip": client_ip(request) if request else "",
            "created_at": utcnow(),
            "expires_at": utcnow() + refresh_ttl,
        }
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


def session_user(user: dict[str, Any], auth: AuthContext) -> SessionUser:
    return SessionUser(
        id=str(auth.user_id),
        email=auth.email,
        full_name=auth.full_name,
        avatar_url=auth.avatar_url,
        scope=auth.scope,
        portal=auth.portal,
        roles=auth.role_names,
        role_keys=auth.role_keys,
        permissions=sorted(auth.permissions),
        is_owner=auth.is_owner,
        student_id=str(auth.student_id) if auth.student_id else None,
        staff_id=str(auth.staff_id) if auth.staff_id else None,
        guardian_id=str(auth.guardian_id) if auth.guardian_id else None,
        last_login_at=user.get("last_login_at"),
    )


async def session_institution(tenant: TenantContext) -> SessionInstitution:
    usage = await _usage_snapshot(tenant)
    year = None
    if tenant.current_academic_year_id:
        doc = await collection(C.ACADEMIC_YEARS).find_one({"_id": tenant.current_academic_year_id})
        year = serialize_doc(doc)
    return SessionInstitution(
        id=str(tenant.id),
        slug=tenant.slug,
        name=tenant.name,
        institution_type=tenant.institution_type,
        deployment=tenant.deployment,
        status=tenant.status,
        subscription_status=tenant.subscription_status,
        subscription_valid_till=(
            tenant.subscription_valid_till.isoformat()
            if tenant.subscription_valid_till else None
        ),
        timezone=tenant.timezone,
        currency=tenant.currency,
        locale=tenant.locale,
        branding=tenant.branding,
        # What this institution actually has, with the plan, the licence and the
        # institution type already folded in. The client should not be running
        # its own half of that rule — that is how the two drift apart.
        enabled_modules=sorted(
            key for key in ALL_MODULE_KEYS if tenant.module_enabled(key)
        ),
        limits={
            "max_students": tenant.limits.max_students,
            "max_staff": tenant.limits.max_staff,
            "max_storage_mb": tenant.limits.max_storage_mb,
            "max_admin_users": tenant.limits.max_admin_users,
        },
        usage=usage,
        current_academic_year=year,
        onboarding_completed=bool(tenant.settings.get("onboarding_completed", True)),
    )


async def _usage_snapshot(tenant: TenantContext) -> dict[str, int]:
    base = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    return {
        "students": await collection(C.STUDENTS).count_documents({**base, "status": "active"}),
        "staff": await collection(C.STAFF).count_documents({**base, "status": "active"}),
        "users": await collection(C.USERS).count_documents({**base, "is_active": True}),
    }


async def build_auth_context(user: dict, tenant_id: ObjectId) -> AuthContext:
    role_ids = [r for r in (user.get("role_ids") or []) if isinstance(r, ObjectId)]
    permissions, keys, names, is_owner, portal = await _permissions_for(tenant_id, role_ids)
    return AuthContext(
        user_id=user["_id"],
        email=user.get("email", ""),
        full_name=user.get("full_name", ""),
        tenant_id=tenant_id,
        role_keys=keys,
        role_names=names,
        permissions=permissions,
        portal=portal,
        is_owner=is_owner,
        student_id=user.get("student_id"),
        staff_id=user.get("staff_id"),
        guardian_id=user.get("guardian_id"),
        avatar_url=user.get("avatar_url", ""),
    )


# ── Institution discovery ─────────────────────────────────────────────────
def _identifier_query(identifier: str) -> dict[str, Any]:
    """Match a login on either the email address or the phone number.

    Digits are compared with separators and a country code stripped, because
    the number a school typed into the student record and the one a parent
    types into the login box are rarely formatted the same way.
    """
    value = identifier.strip()
    digits = "".join(c for c in value if c.isdigit())
    if "@" in value or len(digits) < 6:
        return {"email": value.lower()}
    tail = digits[-10:]
    return {"$or": [{"email": value.lower()}, {"phone_digits": tail}]}


async def find_institutions_for_email(email: str) -> list[InstitutionChoice]:
    """Which institutions this identifier can sign in to (when it's ambiguous)."""
    tenant_ids = await collection(C.USERS).distinct(
        "tenant_id",
        {**_identifier_query(email), "is_active": True, "is_deleted": {"$ne": True}},
    )
    if not tenant_ids:
        return []
    docs = await collection(C.TENANTS).find(
        {"_id": {"$in": tenant_ids}, "is_deleted": {"$ne": True}}
    ).to_list(length=None)
    return [
        InstitutionChoice(
            slug=d.get("slug", ""),
            name=d.get("name", ""),
            institution_type=d.get("institution_type", "school"),
            logo_url=((d.get("branding") or {}).get("logo") or {}).get("url", ""),
        )
        for d in docs
    ]


async def resolve_login_tenant(
    email: str, institution: str | None, tenant: TenantContext | None
) -> TenantContext:
    """Work out which institution a sign-in attempt belongs to."""
    if settings.is_dedicated:
        doc = await load_tenant_doc(slug=settings.dedicated_tenant_slug)
        if doc is None:
            raise Unauthorized("This deployment is not provisioned yet")
        return build_context(doc)

    if institution:
        doc = await load_tenant_doc(slug=institution) or await load_tenant_doc(
            tenant_id=institution
        )
        if doc is None:
            raise Unauthorized("We could not find that institution")
        return build_context(doc)

    if tenant is not None:
        return tenant

    choices = await find_institutions_for_email(email)
    if len(choices) == 1:
        doc = await load_tenant_doc(slug=choices[0].slug)
        return build_context(doc)  # type: ignore[arg-type]
    if len(choices) > 1:
        raise Conflict(
            "This email is registered at more than one institution. Choose one to continue.",
            meta={"institutions": [c.model_dump() for c in choices]},
        )
    raise Unauthorized("Incorrect email or password")


# ── Sign in ───────────────────────────────────────────────────────────────
async def authenticate(
    email: str,
    password: str,
    tenant: TenantContext,
    *,
    request: Request | None = None,
    remember: bool = True,
) -> tuple[dict, AuthContext, TokenPair]:
    users = collection(C.USERS)
    user = await users.find_one({
        **_identifier_query(email),
        "tenant_id": tenant.id, "is_deleted": {"$ne": True},
    })
    if user is None:
        raise Unauthorized("Incorrect email or password")

    locked_until = _aware(user.get("locked_until"))
    if locked_until and locked_until > utcnow():
        minutes = max(1, int((locked_until - utcnow()).total_seconds() // 60) + 1)
        raise Forbidden(f"Too many failed attempts. Try again in {minutes} minute(s).")

    if not verify_password(password, user.get("password_hash", "")):
        attempts = int(user.get("failed_login_attempts", 0)) + 1
        update: dict[str, Any] = {"failed_login_attempts": attempts}
        if attempts >= MAX_FAILED_ATTEMPTS:
            update["locked_until"] = utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            update["failed_login_attempts"] = 0
        await users.update_one({"_id": user["_id"]}, {"$set": update})
        raise Unauthorized("Incorrect email or password")

    if not user.get("is_active", True):
        raise Forbidden("This account has been deactivated. Contact your administrator.")
    if not tenant.is_usable:
        raise Forbidden(
            f"{tenant.name} is currently {tenant.status}. Contact support to restore access."
        )

    await users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "last_login_at": utcnow(),
                "last_login_ip": client_ip(request) if request else "",
                "failed_login_attempts": 0,
                "status": "active",
            },
            "$unset": {"locked_until": ""},
        },
    )
    auth = await build_auth_context(user, tenant.id)
    tokens = await _issue_tokens(user["_id"], tenant_id=tenant.id, request=request,
                                 remember=remember)
    await record(auth, "auth.login", entity_type="user", entity_id=user["_id"],
                 entity_label=auth.email, request=request)
    return user, auth, tokens


async def authenticate_platform(
    email: str, password: str, *, request: Request | None = None
) -> tuple[dict, AuthContext, TokenPair]:
    if not settings.is_saas:
        raise Forbidden("The platform console is not available on this deployment")
    user = await collection(C.PLATFORM_USERS).find_one(
        {"email": email.lower(), "is_deleted": {"$ne": True}}
    )
    if user is None or not verify_password(password, user.get("password_hash", "")):
        raise Unauthorized("Incorrect email or password")
    if not user.get("is_active", True):
        raise Forbidden("This account has been deactivated")

    await collection(C.PLATFORM_USERS).update_one(
        {"_id": user["_id"]}, {"$set": {"last_login_at": utcnow()}}
    )
    role = user.get("role", "platform_support")
    auth = AuthContext(
        user_id=user["_id"],
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
    tokens = await _issue_tokens(user["_id"], scope="platform", request=request)
    await record(auth, "platform.login", entity_type="platform_user",
                 entity_id=user["_id"], entity_label=auth.email, request=request)
    return user, auth, tokens


# ── Refresh / logout ──────────────────────────────────────────────────────
async def rotate_refresh_token(
    token: str, payload: dict, *, request: Request | None = None
) -> TokenPair:
    sessions = collection(C.SESSIONS)
    existing = await sessions.find_one({"token_hash": fingerprint(token), "revoked_at": None})
    if existing is None:
        raise Unauthorized("Session is no longer valid. Sign in again.")
    await sessions.update_one({"_id": existing["_id"]}, {"$set": {"revoked_at": utcnow()}})
    user_id = existing["user_id"]
    scope = existing.get("scope", "tenant")
    tenant_id = existing.get("tenant_id")

    target = C.PLATFORM_USERS if scope == "platform" else C.USERS
    user = await collection(target).find_one({"_id": user_id, "is_deleted": {"$ne": True}})
    if user is None or not user.get("is_active", True):
        raise Unauthorized("Account is no longer active")
    return await _issue_tokens(user_id, scope=scope, tenant_id=tenant_id, request=request)


async def revoke_session(token: str) -> None:
    await collection(C.SESSIONS).update_one(
        {"token_hash": fingerprint(token)}, {"$set": {"revoked_at": utcnow()}}
    )


async def revoke_all_sessions(user_id: ObjectId) -> int:
    result = await collection(C.SESSIONS).update_many(
        {"user_id": user_id, "revoked_at": None}, {"$set": {"revoked_at": utcnow()}}
    )
    return result.modified_count


# ── Passwords ─────────────────────────────────────────────────────────────
def validate_password(password: str) -> None:
    problems = password_problems(password)
    if problems:
        raise ValidationError("Password " + "; ".join(problems))


async def change_password(
    auth: AuthContext, current: str, new: str, *, request: Request | None = None
) -> None:
    target = C.PLATFORM_USERS if auth.is_platform else C.USERS
    query: dict[str, Any] = {"_id": auth.user_id}
    if not auth.is_platform:
        query["tenant_id"] = auth.tenant_id
    user = await collection(target).find_one(query)
    if user is None or not verify_password(current, user.get("password_hash", "")):
        raise Unauthorized("Your current password is incorrect")
    validate_password(new)
    if verify_password(new, user.get("password_hash", "")):
        raise ValidationError("Choose a password you have not used here before")
    await collection(target).update_one(
        {"_id": auth.user_id},
        {"$set": {"password_hash": hash_password(new), "must_change_password": False,
                  "updated_at": utcnow()}},
    )
    await revoke_all_sessions(auth.user_id)
    await record(auth, "auth.password_changed", entity_type="user",
                 entity_id=auth.user_id, request=request)


async def start_password_reset(email: str, tenant: TenantContext) -> str | None:
    """Returns the reset token. Emailing it is the caller's job."""
    user = await collection(C.USERS).find_one(
        {"email": email.lower(), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if user is None:
        return None  # never reveal whether the address exists
    token = create_token(str(user["_id"]), "reset", tenant_id=str(tenant.id))
    await collection(C.USERS).update_one(
        {"_id": user["_id"]},
        {"$set": {"reset_token_hash": fingerprint(token), "reset_requested_at": utcnow()}},
    )
    return token


async def email_password_reset(email: str, token: str) -> None:
    from app.modules.communication.account_mail import send_password_reset

    await send_password_reset(to=email, token=token)


async def finish_password_reset(token: str, new_password: str) -> None:
    from app.core.security import decode_token

    payload = decode_token(token, "reset")
    if payload is None:
        raise Unauthorized("This reset link has expired. Request a new one.")
    validate_password(new_password)
    user_id = ObjectId(payload["sub"])
    user = await collection(C.USERS).find_one({"_id": user_id})
    if user is None or user.get("reset_token_hash") != fingerprint(token):
        raise Unauthorized("This reset link is no longer valid")
    await collection(C.USERS).update_one(
        {"_id": user_id},
        {
            "$set": {"password_hash": hash_password(new_password), "status": "active",
                     "must_change_password": False, "updated_at": utcnow()},
            "$unset": {"reset_token_hash": "", "reset_requested_at": "", "locked_until": ""},
        },
    )
    await revoke_all_sessions(user_id)


async def accept_invite(token: str, password: str, full_name: str | None = None) -> dict:
    from app.core.security import decode_token

    payload = decode_token(token, "invite")
    if payload is None:
        raise Unauthorized("This invitation has expired. Ask for a new one.")
    validate_password(password)
    user_id = ObjectId(payload["sub"])
    user = await collection(C.USERS).find_one({"_id": user_id})
    if user is None or user.get("invite_token_hash") != fingerprint(token):
        raise Unauthorized("This invitation is no longer valid")
    update: dict[str, Any] = {
        "password_hash": hash_password(password),
        "status": "active",
        "is_active": True,
        "must_change_password": False,
        "updated_at": utcnow(),
    }
    if full_name:
        update["full_name"] = full_name
    await collection(C.USERS).update_one(
        {"_id": user_id}, {"$set": update, "$unset": {"invite_token_hash": ""}}
    )
    return await collection(C.USERS).find_one({"_id": user_id})  # type: ignore[return-value]


async def tenant_for_user(user: dict) -> TenantContext:
    """The institution a user belongs to, read from the record rather than the
    request — used where there is no session or header to go on."""
    doc = await load_tenant_doc(tenant_id=str(user.get("tenant_id")))
    if doc is None:
        raise Unauthorized("This account's institution is no longer available")
    return build_context(doc)


async def invite_preview(token: str) -> dict[str, Any]:
    """What the invitation screen needs before a password exists.

    Deliberately thin: a first name, the masked address it was sent to and the
    institution. Anyone holding the link already has the link.
    """
    from app.core.security import decode_token

    payload = decode_token(token, "invite")
    invalid = {"valid": False,
               "detail": "This invitation has expired or has already been used."}
    if payload is None:
        return invalid

    user = await collection(C.USERS).find_one({"_id": ObjectId(payload["sub"])})
    if user is None or user.get("invite_token_hash") != fingerprint(token):
        return invalid

    tenant = await load_tenant_doc(tenant_id=str(user.get("tenant_id")))
    role_names = [
        r.get("name", "")
        for r in await collection(C.ROLES).find(
            {"_id": {"$in": user.get("role_ids") or []}}
        ).to_list(length=None)
    ]
    return {
        "valid": True,
        "full_name": user.get("full_name", ""),
        "email": mask_email(user.get("email", "")),
        "roles": role_names,
        "institution": {
            "name": (tenant or {}).get("name", ""),
            "slug": (tenant or {}).get("slug", ""),
            "logo_url": (((tenant or {}).get("branding") or {}).get("logo") or {}).get("url", ""),
        },
    }


def mask_email(email: str) -> str:
    """``r****a@gmail.com`` — enough to recognise, not enough to harvest."""
    name, _, domain = email.partition("@")
    if not domain:
        return email
    if len(name) <= 2:
        return f"{name[:1]}*@{domain}"
    return f"{name[0]}{'*' * (len(name) - 2)}{name[-1]}@{domain}"


async def create_invite_token(user_id: ObjectId, tenant_id: ObjectId) -> str:
    token = create_token(str(user_id), "invite", tenant_id=str(tenant_id))
    await collection(C.USERS).update_one(
        {"_id": user_id},
        {"$set": {"invite_token_hash": fingerprint(token), "invited_at": utcnow(),
                  "status": "invited"}},
    )
    return token


def temporary_password() -> str:
    """Readable one-off password for bulk-created logins."""
    return f"Sch{random_token(4)[:6].replace('-', 'x').replace('_', 'y')}@{utcnow().year}"


__all__ = [
    "accept_invite", "authenticate", "authenticate_platform", "build_auth_context",
    "build_navigation", "change_password", "create_invite_token",
    "email_password_reset", "find_institutions_for_email", "finish_password_reset",
    "resolve_login_tenant",
    "revoke_all_sessions", "revoke_session", "rotate_refresh_token", "session_institution",
    "session_user", "start_password_reset", "temporary_password", "validate_password",
]
