"""Standing up a new institution.

The same function runs for both deployment shapes. A SaaS signup and a
dedicated install differ only in what gets written into ``deployment``,
``limits`` and ``enabled_modules`` — the data that follows is identical, which
is what makes "lift this school out onto its own server" a copy rather than a
migration.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from bson import ObjectId

from app.core.config import settings
from app.core.permissions import MODULES, ROLE_PRESETS
from app.core.security import generate_license_key, hash_password
from app.core.tenancy import invalidate_tenant_cache
from app.db.mongo import C, collection
from app.models.base import utcnow
from app.models.catalog import PLANS_BY_KEY
from app.models.tenant import Deployment, SubscriptionStatus, Tenant, TenantStatus

log = logging.getLogger("scholarly.provisioning")

ALL_MODULE_KEYS = [m.key for m in MODULES]


class ProvisionResult(dict):
    @property
    def tenant_id(self) -> ObjectId:
        return self["tenant_id"]


async def slug_available(slug: str) -> bool:
    existing = await collection(C.TENANTS).find_one({"slug": slug.strip().lower()})
    return existing is None


def suggest_slug(name: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    while "--" in base:
        base = base.replace("--", "-")
    return base[:40] or "institution"


async def unique_slug(name: str) -> str:
    base = suggest_slug(name)
    candidate, n = base, 1
    while not await slug_available(candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


async def provision_tenant(
    *,
    name: str,
    slug: str | None = None,
    owner_email: str,
    owner_name: str = "",
    owner_password: str | None = None,
    institution_type: str = "school",
    plan_key: str = "trial",
    deployment: str = "saas",
    contact: dict[str, Any] | None = None,
    address: dict[str, Any] | None = None,
    timezone: str = "Asia/Kolkata",
    currency: str = "INR",
    academic_year_start_month: int = 4,
    enabled_modules: list[str] | None = None,
    created_by: ObjectId | None = None,
) -> ProvisionResult:
    slug = (slug or await unique_slug(name)).strip().lower()
    if not await slug_available(slug):
        from app.core.exceptions import Conflict

        raise Conflict(f"The address '{slug}' is already taken")

    dedicated = deployment == Deployment.DEDICATED
    plan = PLANS_BY_KEY.get("lifetime" if dedicated else plan_key, PLANS_BY_KEY["trial"])
    modules = (
        list(ALL_MODULE_KEYS)
        if dedicated
        else (enabled_modules or list(plan["included_modules"]))
    )
    trial_days = 0 if dedicated else int(plan.get("trial_days", 0))
    today = date.today()

    tenant = Tenant(
        slug=slug,
        name=name.strip(),
        institution_type=institution_type,  # type: ignore[arg-type]
        status=TenantStatus.ACTIVE if (dedicated or not trial_days) else TenantStatus.TRIAL,
        deployment=Deployment.DEDICATED if dedicated else Deployment.SAAS,
        plan_key="lifetime" if dedicated else plan_key,
        subscription_status=(
            SubscriptionStatus.LIFETIME if dedicated
            else (SubscriptionStatus.TRIALING if trial_days else SubscriptionStatus.ACTIVE)
        ),
        trial_ends_at=today + timedelta(days=trial_days) if trial_days else None,
        subscription_valid_till=(
            None if dedicated else today + timedelta(days=trial_days or 365)
        ),
        limits={} if dedicated else plan.get("limits", {}),  # type: ignore[arg-type]
        enabled_modules=modules,
        timezone=timezone,
        currency=currency,
        academic_year_start_month=academic_year_start_month,
        license_key=generate_license_key(slug) if dedicated else "",
        licensed_at=utcnow() if dedicated else None,
        contact=contact or {},  # type: ignore[arg-type]
        address=address or {},  # type: ignore[arg-type]
        created_by=created_by,
    )
    doc = tenant.to_mongo()
    result = await collection(C.TENANTS).insert_one(doc)
    tenant_id: ObjectId = result.inserted_id
    log.info("Provisioned tenant %s (%s) as %s", name, slug, tenant.deployment)

    role_ids = await seed_roles(tenant_id)
    owner_role_id = role_ids["super_admin"]

    password = owner_password or settings.platform_owner_password
    owner_id = await create_owner_user(
        tenant_id, owner_email, owner_name or name, password, owner_role_id
    )

    year_id = await seed_academic_year(tenant_id, academic_year_start_month, owner_id)
    await collection(C.TENANTS).update_one(
        {"_id": tenant_id}, {"$set": {"current_academic_year_id": year_id}}
    )

    await seed_defaults(tenant_id, owner_id)

    if not dedicated:
        await create_subscription(tenant_id, plan_key, trial_days)

    invalidate_tenant_cache()
    return ProvisionResult(
        tenant_id=tenant_id,
        slug=slug,
        owner_user_id=owner_id,
        owner_email=owner_email.lower(),
        owner_password=password,
        academic_year_id=year_id,
        license_key=tenant.license_key,
        role_ids=role_ids,
    )


async def seed_roles(tenant_id: ObjectId) -> dict[str, ObjectId]:
    """Create the preset roles. Already-present keys are left untouched so this
    is safe to re-run after we ship new presets."""
    roles = collection(C.ROLES)
    out: dict[str, ObjectId] = {}
    for preset in ROLE_PRESETS:
        existing = await roles.find_one({"tenant_id": tenant_id, "key": preset.key})
        if existing:
            out[preset.key] = existing["_id"]
            continue
        doc = {
            "tenant_id": tenant_id,
            "key": preset.key,
            "name": preset.name,
            "description": preset.description,
            "permissions": preset.permissions,
            "portal": preset.portal,
            "is_system": True,
            "is_owner": preset.is_owner,
            "user_count": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "is_deleted": False,
        }
        out[preset.key] = (await roles.insert_one(doc)).inserted_id
    return out


async def create_owner_user(
    tenant_id: ObjectId, email: str, full_name: str, password: str, role_id: ObjectId
) -> ObjectId:
    users = collection(C.USERS)
    email = email.strip().lower()
    existing = await users.find_one({"tenant_id": tenant_id, "email": email})
    if existing:
        return existing["_id"]
    doc = {
        "tenant_id": tenant_id,
        "email": email,
        "full_name": full_name.strip(),
        "password_hash": hash_password(password),
        "role_ids": [role_id],
        "status": "active",
        "is_active": True,
        "must_change_password": True,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "is_deleted": False,
    }
    user_id = (await users.insert_one(doc)).inserted_id
    await collection(C.ROLES).update_one({"_id": role_id}, {"$inc": {"user_count": 1}})
    return user_id


async def seed_academic_year(
    tenant_id: ObjectId, start_month: int, actor_id: ObjectId | None = None
) -> ObjectId:
    today = date.today()
    start_year = today.year if today.month >= start_month else today.year - 1
    start = date(start_year, start_month, 1)
    end = date(start_year + 1, start_month, 1) - timedelta(days=1)
    name = f"{start_year}-{str(start_year + 1)[-2:]}"

    years = collection(C.ACADEMIC_YEARS)
    existing = await years.find_one({"tenant_id": tenant_id, "name": name})
    if existing:
        return existing["_id"]
    doc = {
        "tenant_id": tenant_id,
        "name": name,
        "start_date": datetime(start.year, start.month, start.day, tzinfo=UTC),
        "end_date": datetime(end.year, end.month, end.day, tzinfo=UTC),
        "is_current": True,
        "status": "active",
        "created_by": actor_id,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "is_deleted": False,
    }
    return (await years.insert_one(doc)).inserted_id


DEFAULT_GRADE_SCALE = [
    {"grade": "A+", "min": 90, "max": 100, "points": 10, "remark": "Outstanding"},
    {"grade": "A", "min": 80, "max": 89.99, "points": 9, "remark": "Excellent"},
    {"grade": "B+", "min": 70, "max": 79.99, "points": 8, "remark": "Very Good"},
    {"grade": "B", "min": 60, "max": 69.99, "points": 7, "remark": "Good"},
    {"grade": "C+", "min": 50, "max": 59.99, "points": 6, "remark": "Above Average"},
    {"grade": "C", "min": 40, "max": 49.99, "points": 5, "remark": "Average"},
    {"grade": "D", "min": 33, "max": 39.99, "points": 4, "remark": "Needs Improvement"},
    {"grade": "F", "min": 0, "max": 32.99, "points": 0, "remark": "Not Cleared"},
]

DEFAULT_FEE_HEADS = [
    {"code": "TUITION", "name": "Tuition Fee", "category": "academic", "is_recurring": True},
    {"code": "ADMISSION", "name": "Admission Fee", "category": "one_time", "is_recurring": False},
    {"code": "EXAM", "name": "Examination Fee", "category": "academic", "is_recurring": False},
    {"code": "LIBRARY", "name": "Library Fee", "category": "facility", "is_recurring": True},
    {"code": "LAB", "name": "Laboratory Fee", "category": "facility", "is_recurring": True},
    {"code": "TRANSPORT", "name": "Transport Fee", "category": "facility", "is_recurring": True},
    {"code": "HOSTEL", "name": "Hostel Fee", "category": "facility", "is_recurring": True},
    {"code": "SPORTS", "name": "Sports & Activities", "category": "facility", "is_recurring": True},
    {"code": "LATE", "name": "Late Fee Fine", "category": "penalty", "is_recurring": False},
]

DEFAULT_LEAVE_TYPES = [
    {"code": "CL", "name": "Casual Leave", "annual_quota": 12, "is_paid": True},
    {"code": "SL", "name": "Sick Leave", "annual_quota": 10, "is_paid": True},
    {"code": "EL", "name": "Earned Leave", "annual_quota": 15, "is_paid": True},
    {"code": "ML", "name": "Maternity Leave", "annual_quota": 180, "is_paid": True},
    {"code": "LWP", "name": "Leave Without Pay", "annual_quota": 0, "is_paid": False},
]

DEFAULT_PERIODS = [
    {"name": "Period 1", "start_time": "09:00", "end_time": "09:45", "order": 1},
    {"name": "Period 2", "start_time": "09:45", "end_time": "10:30", "order": 2},
    {"name": "Short Break", "start_time": "10:30", "end_time": "10:45", "order": 3,
     "is_break": True},
    {"name": "Period 3", "start_time": "10:45", "end_time": "11:30", "order": 4},
    {"name": "Period 4", "start_time": "11:30", "end_time": "12:15", "order": 5},
    {"name": "Lunch", "start_time": "12:15", "end_time": "13:00", "order": 6, "is_break": True},
    {"name": "Period 5", "start_time": "13:00", "end_time": "13:45", "order": 7},
    {"name": "Period 6", "start_time": "13:45", "end_time": "14:30", "order": 8},
    {"name": "Period 7", "start_time": "14:30", "end_time": "15:15", "order": 9},
]


async def seed_defaults(tenant_id: ObjectId, actor_id: ObjectId | None = None) -> None:
    """Sensible starting data so a new institution is usable on day one."""
    stamp = {
        "tenant_id": tenant_id, "created_by": actor_id, "created_at": utcnow(),
        "updated_at": utcnow(), "is_deleted": False,
    }

    if not await collection(C.GRADE_SCALES).find_one({"tenant_id": tenant_id}):
        await collection(C.GRADE_SCALES).insert_one(
            {**stamp, "name": "Default Grading Scale", "is_default": True,
             "bands": DEFAULT_GRADE_SCALE, "pass_percentage": 33}
        )

    if not await collection(C.FEE_HEADS).find_one({"tenant_id": tenant_id}):
        await collection(C.FEE_HEADS).insert_many(
            [{**stamp, **head, "is_active": True} for head in DEFAULT_FEE_HEADS]
        )

    if not await collection(C.LEAVE_TYPES).find_one({"tenant_id": tenant_id}):
        await collection(C.LEAVE_TYPES).insert_many(
            [{**stamp, **lt, "is_active": True} for lt in DEFAULT_LEAVE_TYPES]
        )

    year = await collection(C.ACADEMIC_YEARS).find_one(
        {"tenant_id": tenant_id, "is_current": True}
    )
    if year and not await collection(C.PERIODS).find_one({"tenant_id": tenant_id}):
        await collection(C.PERIODS).insert_many(
            [{**stamp, **p, "academic_year_id": year["_id"], "is_break": p.get("is_break", False)}
             for p in DEFAULT_PERIODS]
        )


async def create_subscription(tenant_id: ObjectId, plan_key: str, trial_days: int) -> ObjectId:
    plan = PLANS_BY_KEY.get(plan_key, PLANS_BY_KEY["trial"])
    today = date.today()
    period_end = today + timedelta(days=trial_days or 365)
    doc = {
        "tenant_id": tenant_id,
        "plan_key": plan_key,
        "status": "trialing" if trial_days else "active",
        "billing_cycle": "yearly",
        "amount": float(plan.get("price_yearly", 0)),
        "currency": "INR",
        "started_at": utcnow(),
        "current_period_start": datetime(today.year, today.month, today.day, tzinfo=UTC),
        "current_period_end": datetime(
            period_end.year, period_end.month, period_end.day, tzinfo=UTC
        ),
        "cancel_at_period_end": False,
        "history": [{"at": utcnow(), "event": "created", "plan_key": plan_key}],
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "is_deleted": False,
    }
    return (await collection(C.SUBSCRIPTIONS).insert_one(doc)).inserted_id


async def convert_to_dedicated(tenant_id: ObjectId) -> dict[str, Any]:
    """Flip an existing SaaS institution to a lifetime / self-owned licence.

    The institution keeps its ``_id`` and every document it already owns, so its
    data can then be exported wholesale into its own deployment.
    """
    tenant = await collection(C.TENANTS).find_one({"_id": tenant_id})
    if tenant is None:
        from app.core.exceptions import NotFound

        raise NotFound("Institution not found")

    license_key = generate_license_key(tenant["slug"])
    await collection(C.TENANTS).update_one(
        {"_id": tenant_id},
        {
            "$set": {
                "deployment": "dedicated",
                "plan_key": "lifetime",
                "status": "active",
                "subscription_status": "lifetime",
                "subscription_valid_till": None,
                "limits": {},
                "enabled_modules": list(ALL_MODULE_KEYS),
                "license_key": license_key,
                "licensed_at": utcnow(),
                "updated_at": utcnow(),
            }
        },
    )
    await collection(C.SUBSCRIPTIONS).update_many(
        {"tenant_id": tenant_id, "status": {"$in": ["trialing", "active", "past_due"]}},
        {"$set": {"status": "lifetime", "updated_at": utcnow()}},
    )
    invalidate_tenant_cache()
    return {"license_key": license_key, "slug": tenant["slug"]}
