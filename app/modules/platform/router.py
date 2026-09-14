"""The company console. Mounted only when DEPLOYMENT_MODE=saas.

A dedicated institution's server never exposes these routes at all, so there is
no company back door into a school that bought its own deployment.
"""

from __future__ import annotations

from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, Query, Request, status

from app.core.context import AuthContext
from app.core.deps import require_platform
from app.core.exceptions import Forbidden, ValidationError
from app.core.permissions import module_group_tree
from app.models.base import Msg
from app.modules.platform import service
from app.modules.platform.schemas import (
    ImpersonateRequest,
    PlanUpsertRequest,
    SignupRequest,
    SubscriptionChangeRequest,
    TenantCreateRequest,
    TenantStatusRequest,
    TenantUpdateRequest,
)
from app.modules.tenants.provisioning import convert_to_dedicated, provision_tenant
from app.utils.audit import record

router = APIRouter(prefix="/platform", tags=["Platform Console"])

Admin = Annotated[AuthContext, Depends(require_platform("tenants:read"))]
Writer = Annotated[AuthContext, Depends(require_platform("tenants:*"))]
Biller = Annotated[AuthContext, Depends(require_platform("subscriptions:*"))]
Planner = Annotated[AuthContext, Depends(require_platform("plans:*"))]


# ── Overview ──────────────────────────────────────────────────────────────
@router.get("/metrics", summary="Business overview")
async def metrics(auth: Annotated[AuthContext, Depends(require_platform("metrics:read"))]):
    return await service.platform_metrics()


@router.get("/modules", summary="Module catalogue for plan editing")
async def modules(auth: Admin):
    return {"groups": module_group_tree()}


# ── Institutions ──────────────────────────────────────────────────────────
@router.get("/tenants", summary="List institutions")
async def list_tenants(
    auth: Admin,
    page: int = 1,
    page_size: Annotated[int, Query(le=200)] = 25,
    search: str = "",
    status_filter: Annotated[str, Query(alias="status")] = "",
    deployment: str = "",
    plan_key: str = "",
    sort_by: str = "created_at",
    sort_dir: str = "desc",
):
    return await service.list_tenants(
        page=page, page_size=page_size, search=search, status=status_filter,
        deployment=deployment, plan_key=plan_key, sort_by=sort_by, sort_dir=sort_dir,
    )


@router.get("/tenants/slug-available", summary="Check an address is free")
async def slug_available(auth: Admin, slug: str):
    return await service.check_slug(slug)


@router.post("/tenants", status_code=status.HTTP_201_CREATED, summary="Provision an institution")
async def create_tenant(payload: TenantCreateRequest, auth: Writer, request: Request):
    """Creates the institution, its role set, its owner login and its first
    academic year.

    ``deployment=dedicated`` provisions it as self-owned: no plan limits, every
    module on, and a licence key it can carry to its own server.
    """
    result = await provision_tenant(
        name=payload.name,
        slug=payload.slug,
        owner_email=payload.owner_email,
        owner_name=payload.owner_name,
        owner_password=payload.owner_password,
        institution_type=payload.institution_type,
        plan_key=payload.plan_key,
        deployment=payload.deployment,
        contact={"email": payload.owner_email, "phone": payload.phone},
        address={"city": payload.city, "state": payload.state, "country": payload.country},
        timezone=payload.timezone,
        currency=payload.currency,
        academic_year_start_month=payload.academic_year_start_month,
        enabled_modules=payload.enabled_modules,
        created_by=auth.user_id,
    )
    await record(auth, "tenant.provisioned", entity_type="tenant",
                 entity_id=result["tenant_id"], entity_label=payload.name, request=request)
    return {
        "id": str(result["tenant_id"]),
        "slug": result["slug"],
        "owner_email": result["owner_email"],
        "owner_password": result["owner_password"],
        "license_key": result["license_key"],
        "detail": f"{payload.name} is ready",
    }


@router.get("/tenants/{tenant_id}", summary="Institution detail")
async def tenant_detail(tenant_id: str, auth: Admin):
    return await service.get_tenant_detail(tenant_id)


@router.patch("/tenants/{tenant_id}", summary="Update an institution")
async def update_tenant(
    tenant_id: str, payload: TenantUpdateRequest, auth: Writer, request: Request
):
    doc = await service.update_tenant(
        ObjectId(tenant_id), payload.model_dump(exclude_none=True)
    )
    await record(auth, "tenant.updated", entity_type="tenant", entity_id=tenant_id,
                 entity_label=doc.get("name", ""), request=request)
    return doc


@router.post("/tenants/{tenant_id}/status", summary="Suspend, restore or cancel")
async def set_status(
    tenant_id: str, payload: TenantStatusRequest, auth: Writer, request: Request
):
    doc = await service.set_tenant_status(ObjectId(tenant_id), payload.status, payload.reason)
    await record(auth, f"tenant.{payload.status}", entity_type="tenant", entity_id=tenant_id,
                 entity_label=doc.get("name", ""), changes={"reason": payload.reason},
                 request=request)
    return doc


@router.post("/tenants/{tenant_id}/convert-to-dedicated",
             summary="Move an institution onto its own lifetime licence")
async def convert_dedicated(tenant_id: str, auth: Writer, request: Request):
    """Flips a subscribing institution to self-owned.

    Its data keeps the same ids, so exporting it into its own deployment is a
    copy rather than a migration. Plan limits and module gating stop applying
    immediately.
    """
    result = await convert_to_dedicated(ObjectId(tenant_id))
    await record(auth, "tenant.converted_to_dedicated", entity_type="tenant",
                 entity_id=tenant_id, entity_label=result["slug"], request=request)
    return {
        **result,
        "detail": "Converted to a dedicated licence. Every module is now unlocked.",
    }


@router.post("/tenants/{tenant_id}/subscription", summary="Change plan or renew")
async def change_subscription(
    tenant_id: str, payload: SubscriptionChangeRequest, auth: Biller, request: Request
):
    doc = await service.change_subscription(
        ObjectId(tenant_id),
        payload.plan_key,
        billing_cycle=payload.billing_cycle,
        amount=payload.amount,
        valid_till=payload.valid_till,
        payment_reference=payload.payment_reference,
        note=payload.note,
        actor=auth.email,
    )
    await record(auth, "subscription.changed", entity_type="tenant", entity_id=tenant_id,
                 changes={"plan_key": payload.plan_key}, request=request)
    return doc


@router.post("/tenants/{tenant_id}/impersonate",
             summary="Open a 30-minute support session inside an institution")
async def impersonate(
    tenant_id: str,
    payload: ImpersonateRequest,
    auth: Annotated[AuthContext, Depends(require_platform("tenants:impersonate"))],
    request: Request,
):
    tenant = await service.get_tenant_detail(tenant_id)
    if tenant.get("deployment") == "dedicated":
        raise Forbidden(
            "This institution runs its own dedicated licence. Support access must be "
            "granted by them."
        )
    result = await service.impersonation_token(ObjectId(tenant_id), auth.email)
    await record(auth, "tenant.impersonated", entity_type="tenant", entity_id=tenant_id,
                 entity_label=tenant.get("name", ""), changes={"reason": payload.reason},
                 request=request)
    # Also written into the institution's own trail, so they can see it happened.
    await record(auth, "support.session_opened", entity_type="tenant", entity_id=tenant_id,
                 changes={"by": auth.email, "reason": payload.reason},
                 tenant_id=ObjectId(tenant_id), request=request)
    return result


# ── Subscriptions & plans ─────────────────────────────────────────────────
@router.get("/subscriptions", summary="All subscriptions")
async def subscriptions(
    auth: Annotated[AuthContext, Depends(require_platform("subscriptions:read"))],
    status_filter: Annotated[str, Query(alias="status")] = "",
    expiring_days: int | None = None,
):
    return await service.list_subscriptions(status=status_filter, expiring_days=expiring_days)


@router.get("/plans", summary="All plans")
async def plans(auth: Annotated[AuthContext, Depends(require_platform("plans:read"))]):
    return await service.list_plans()


@router.put("/plans", summary="Create or update a plan")
async def upsert_plan(payload: PlanUpsertRequest, auth: Planner, request: Request):
    doc = await service.upsert_plan(payload.model_dump())
    await record(auth, "plan.saved", entity_type="plan", entity_label=payload.key,
                 request=request)
    return doc


@router.delete("/plans/{key}", response_model=Msg, summary="Retire a plan")
async def delete_plan(key: str, auth: Planner, request: Request):
    await service.delete_plan(key)
    await record(auth, "plan.deleted", entity_type="plan", entity_label=key, request=request)
    return Msg(detail=f"Plan '{key}' retired")


# ── Public (no auth) ──────────────────────────────────────────────────────
public_router = APIRouter(prefix="/public", tags=["Public"])


@public_router.get("/plans", summary="Plans shown on the pricing page")
async def public_plans():
    return await service.list_plans(public_only=True)


@public_router.get("/slug-available", summary="Check an address during signup")
async def public_slug(slug: str):
    return await service.check_slug(slug)


@public_router.post("/signup", status_code=status.HTTP_201_CREATED,
                    summary="Start a free trial")
async def signup(payload: SignupRequest, request: Request):
    from app.modules.auth.service import validate_password

    validate_password(payload.password)
    if payload.plan_key == "lifetime":
        raise ValidationError(
            "A dedicated licence is arranged with our team. Use the contact form instead."
        )
    result = await provision_tenant(
        name=payload.institution_name,
        slug=payload.preferred_slug,
        owner_email=payload.email,
        owner_name=payload.full_name,
        owner_password=payload.password,
        institution_type=payload.institution_type,
        plan_key=payload.plan_key or "trial",
        deployment="saas",
        contact={"email": payload.email, "phone": payload.phone},
        address={"city": payload.city},
    )
    await record(None, "tenant.self_signup", entity_type="tenant",
                 entity_id=result["tenant_id"], entity_label=payload.institution_name,
                 request=request)
    return {
        "id": str(result["tenant_id"]),
        "slug": result["slug"],
        "detail": f"Welcome aboard. {payload.institution_name} is ready.",
    }
