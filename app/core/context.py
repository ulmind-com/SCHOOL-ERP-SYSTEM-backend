"""Per-request identity: which institution, which user, what they may do."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from bson import ObjectId

from app.core.config import settings
from app.core.permissions import CORE_MODULE_KEYS, MODULES_BY_KEY, has_permission


@dataclass(slots=True)
class PlanLimits:
    """Ceilings applied in SaaS mode. In dedicated mode every value is ``None``
    (unlimited) — the institution bought the software, not a seat count."""

    max_students: int | None = None
    max_staff: int | None = None
    max_storage_mb: int | None = None
    max_admin_users: int | None = None

    @classmethod
    def unlimited(cls) -> PlanLimits:
        return cls()

    def ceiling(self, key: str) -> int | None:
        return getattr(self, key, None)


@dataclass(slots=True)
class TenantContext:
    """The institution this request belongs to."""

    id: ObjectId
    slug: str
    name: str
    institution_type: str = "school"  # school | college | university | institute
    status: str = "active"  # active | trial | suspended | cancelled
    deployment: str = "saas"  # saas | dedicated
    enabled_modules: set[str] = field(default_factory=set)
    limits: PlanLimits = field(default_factory=PlanLimits.unlimited)
    plan_key: str = ""
    subscription_status: str = "active"
    subscription_valid_till: date | None = None
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    locale: str = "en-IN"
    branding: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    current_academic_year_id: ObjectId | None = None
    #: Carried so printed documents can render a real letterhead without a
    #: second database read on every receipt.
    address_line: str = ""
    contact_line: str = ""

    @property
    def is_dedicated(self) -> bool:
        return self.deployment == "dedicated"

    @property
    def is_usable(self) -> bool:
        """Suspended / cancelled institutions can authenticate but not write."""
        return self.status in {"active", "trial"}

    def module_enabled(self, key: str) -> bool:
        # Institution type comes first, and a dedicated licence does not buy
        # past it: a school that owns the whole deployment still has no
        # Faculties, and showing it one is not generosity.
        module = MODULES_BY_KEY.get(key)
        if module is not None and not module.suits(self.institution_type):
            return False
        # Otherwise a dedicated deployment owns everything; nothing is withheld.
        if self.is_dedicated:
            return True
        return key in CORE_MODULE_KEYS or key in self.enabled_modules

    def storage_path(self) -> str:
        """ImageKit folder — one per institution, so a dedicated school's media
        can be handed over as a single folder."""
        return f"/{'dedicated' if self.is_dedicated else 'tenants'}/{self.slug}"


@dataclass(slots=True)
class AuthContext:
    """The authenticated principal."""

    user_id: ObjectId
    email: str
    full_name: str
    scope: str = "tenant"  # tenant | platform
    tenant_id: ObjectId | None = None
    role_keys: list[str] = field(default_factory=list)
    role_names: list[str] = field(default_factory=list)
    permissions: set[str] = field(default_factory=set)
    portal: str = "admin"  # admin | teacher | student | parent | finance | platform
    is_owner: bool = False
    #: Set when the user is a student / staff member / guardian themselves.
    student_id: ObjectId | None = None
    staff_id: ObjectId | None = None
    guardian_id: ObjectId | None = None
    #: Someone is acting as this user — platform support inside a tenant, or an
    #: institution admin opening a student's portal to see what they see.
    impersonating: bool = False
    #: Who is really at the keyboard. Every write is attributed to them as well
    #: as to the account being used, so "who changed this" has one answer.
    impersonated_by_id: ObjectId | None = None
    impersonated_by_name: str = ""
    avatar_url: str = ""

    @property
    def is_platform(self) -> bool:
        return self.scope == "platform"

    def can(self, permission: str) -> bool:
        return has_permission(self.permissions, permission)

    def can_any(self, *permissions: str) -> bool:
        return any(self.can(p) for p in permissions)

    def can_all(self, *permissions: str) -> bool:
        return all(self.can(p) for p in permissions)


def dedicated_tenant_defaults() -> dict[str, Any]:
    """Seed values for the single institution of a dedicated deployment."""
    return {
        "slug": settings.dedicated_tenant_slug,
        "name": settings.dedicated_tenant_name or settings.dedicated_tenant_slug.title(),
        "deployment": "dedicated",
        "status": "active",
        "plan_key": "lifetime",
        "subscription_status": "lifetime",
        "license_key": settings.dedicated_license_key,
    }
