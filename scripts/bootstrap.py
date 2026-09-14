"""Idempotent bootstrap: plans, platform owner, and (optionally) the single
institution of a dedicated deployment."""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.security import hash_password
from app.db.mongo import C, collection
from app.models.base import utcnow
from app.models.catalog import DEFAULT_PLANS
from app.modules.tenants.provisioning import provision_tenant

log = logging.getLogger("scholarly.bootstrap")


async def seed_plans() -> int:
    """Upsert the catalogue. Prices already edited in the console are kept."""
    plans = collection(C.PLANS)
    written = 0
    for plan in DEFAULT_PLANS:
        existing = await plans.find_one({"key": plan["key"]})
        if existing:
            continue
        await plans.insert_one(
            {**plan, "is_active": True, "is_public": plan.get("is_public", True),
             "currency": "INR", "created_at": utcnow(), "updated_at": utcnow(),
             "is_deleted": False}
        )
        written += 1
    return written


async def seed_platform_owner() -> dict | None:
    """The company's first login. SaaS deployments only."""
    if not settings.is_saas:
        return None
    users = collection(C.PLATFORM_USERS)
    email = settings.platform_owner_email.lower()
    if await users.find_one({"email": email}):
        return {"email": email, "created": False}
    await users.insert_one(
        {
            "email": email,
            "full_name": settings.platform_owner_name,
            "password_hash": hash_password(settings.platform_owner_password),
            "role": "platform_owner",
            "is_active": True,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "is_deleted": False,
        }
    )
    return {"email": email, "password": settings.platform_owner_password, "created": True}


async def seed_dedicated_tenant() -> dict | None:
    """On a dedicated deployment, make sure the owning institution exists."""
    if not settings.is_dedicated:
        return None
    slug = settings.dedicated_tenant_slug
    existing = await collection(C.TENANTS).find_one({"slug": slug})
    if existing:
        return {"slug": slug, "created": False, "tenant_id": str(existing["_id"])}
    result = await provision_tenant(
        name=settings.dedicated_tenant_name or slug.replace("-", " ").title(),
        slug=slug,
        owner_email=settings.platform_owner_email,
        owner_name=settings.platform_owner_name,
        owner_password=settings.platform_owner_password,
        deployment="dedicated",
        plan_key="lifetime",
    )
    return {
        "slug": slug,
        "created": True,
        "tenant_id": str(result["tenant_id"]),
        "owner_email": result["owner_email"],
        "owner_password": result["owner_password"],
        "license_key": result["license_key"],
    }


async def sync_roles_everywhere() -> dict:
    """Bring every institution's untouched built-in roles up to the current
    presets. Runs on boot because a permission we have since decided is wrong
    otherwise stays granted at every school already using the product."""
    from app.core.tenancy import build_context
    from app.db.mongo import C, collection
    from app.modules.users.service import sync_builtin_roles

    updated, institutions = 0, 0
    async for doc in collection(C.TENANTS).find({"is_deleted": {"$ne": True}}):
        result = await sync_builtin_roles(build_context(doc))
        if result["roles_updated"]:
            institutions += 1
            updated += result["roles_updated"]
    return {"institutions": institutions, "roles_updated": updated}


async def run() -> dict:
    plans = await seed_plans()
    owner = await seed_platform_owner()
    dedicated = await seed_dedicated_tenant()
    roles = await sync_roles_everywhere()
    return {"plans_seeded": plans, "platform_owner": owner,
            "dedicated_tenant": dedicated, "roles": roles}
