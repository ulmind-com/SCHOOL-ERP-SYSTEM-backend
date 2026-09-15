"""Resolving *which institution* a request belongs to.

Resolution order (first hit wins):

1. ``dedicated`` deployment — always the one institution that owns the server.
2. The ``tid`` claim on the caller's access token.
3. An explicit ``X-Tenant`` header (slug or id) — used on login, before a token
   exists, and by the platform console when acting on an institution.
4. The host's subdomain, when ``TENANT_BASE_DOMAIN`` is configured.
"""

from __future__ import annotations

import time
from typing import Any

from bson import ObjectId

from app.core.config import settings
from app.core.context import PlanLimits, TenantContext
from app.core.exceptions import TenantError
from app.db.mongo import C, collection
from app.models.base import oid

_CACHE_TTL = 30.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def invalidate_tenant_cache(key: str | None = None) -> None:
    if key is None:
        _cache.clear()
    else:
        for k in [k for k in _cache if k.endswith(str(key)) or k == str(key)]:
            _cache.pop(k, None)


async def _load(cache_key: str, query: dict[str, Any]) -> dict[str, Any] | None:
    hit = _cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    doc = await collection(C.TENANTS).find_one({**query, "is_deleted": {"$ne": True}})
    if doc:
        _cache[cache_key] = (time.monotonic() + _CACHE_TTL, doc)
    return doc


async def load_tenant_doc(
    *, tenant_id: str | ObjectId | None = None, slug: str | None = None
) -> dict[str, Any] | None:
    if tenant_id:
        _id = oid(tenant_id)
        if _id is None:
            return None
        return await _load(f"id:{_id}", {"_id": _id})
    if slug:
        slug = slug.strip().lower()
        return await _load(f"slug:{slug}", {"slug": slug})
    return None


def _one_line_address(doc: dict[str, Any]) -> str:
    address = doc.get("address") or {}
    parts = [address.get(k, "") for k in
             ("line1", "line2", "city", "state", "postal_code")]
    return ", ".join(p for p in parts if p)


def _one_line_contact(doc: dict[str, Any]) -> str:
    contact = doc.get("contact") or {}
    parts = [contact.get("phone", ""), contact.get("email", ""), doc.get("website", "")]
    return " · ".join(p for p in parts if p)


def build_context(doc: dict[str, Any]) -> TenantContext:
    plan = doc.get("limits") or {}
    dedicated = doc.get("deployment") == "dedicated" or settings.is_dedicated
    return TenantContext(
        id=doc["_id"],
        slug=doc.get("slug", ""),
        name=doc.get("name", ""),
        institution_type=doc.get("institution_type", "school"),
        status=doc.get("status", "active"),
        deployment="dedicated" if dedicated else "saas",
        enabled_modules=set(doc.get("enabled_modules") or []),
        limits=(
            PlanLimits.unlimited()
            if dedicated
            else PlanLimits(
                max_students=plan.get("max_students"),
                max_staff=plan.get("max_staff"),
                max_storage_mb=plan.get("max_storage_mb"),
                max_admin_users=plan.get("max_admin_users"),
            )
        ),
        plan_key=doc.get("plan_key", ""),
        subscription_status=doc.get("subscription_status", "active"),
        subscription_valid_till=doc.get("subscription_valid_till"),
        timezone=doc.get("timezone", "Asia/Kolkata"),
        currency=doc.get("currency", "INR"),
        locale=doc.get("locale", "en-IN"),
        branding=doc.get("branding") or {},
        settings=doc.get("settings") or {},
        current_academic_year_id=doc.get("current_academic_year_id"),
        active_academic_year_id=doc.get("current_academic_year_id"),
        address_line=_one_line_address(doc),
        contact_line=_one_line_contact(doc),
    )


def subdomain_of(host: str) -> str | None:
    """``stjohns.scholarly.app`` -> ``stjohns`` (when the base domain matches)."""
    base = settings.tenant_base_domain.strip().lower()
    if not base or not host:
        return None
    host = host.split(":")[0].lower()
    if host == base or not host.endswith(f".{base}"):
        return None
    sub = host[: -(len(base) + 1)]
    if sub in {"www", "app", "api", "admin", "platform"} or "." in sub:
        return None
    return sub


async def resolve_tenant(
    *,
    token_tenant_id: str | None = None,
    header_tenant: str | None = None,
    host: str | None = None,
    required: bool = True,
) -> TenantContext | None:
    doc: dict[str, Any] | None = None

    if settings.is_dedicated:
        doc = await load_tenant_doc(slug=settings.dedicated_tenant_slug)
        if doc is None and required:
            raise TenantError(
                "This deployment is not provisioned yet. Run the bootstrap script."
            )
    if doc is None and token_tenant_id:
        doc = await load_tenant_doc(tenant_id=token_tenant_id)
    if doc is None and header_tenant:
        header_tenant = header_tenant.strip()
        doc = await load_tenant_doc(tenant_id=header_tenant) or await load_tenant_doc(
            slug=header_tenant
        )
    if doc is None and host:
        sub = subdomain_of(host)
        if sub:
            doc = await load_tenant_doc(slug=sub)

    if doc is None:
        if required:
            raise TenantError(
                "Institution not identified. Send an X-Tenant header or sign in again."
            )
        return None
    return build_context(doc)
