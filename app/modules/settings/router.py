"""Institution settings — the knobs a school owns without calling us."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.core.config import settings as app_settings
from app.core.context import AuthContext
from app.core.deps import TenantDep, require, require_owner
from app.core.exceptions import Forbidden, ValidationError
from app.core.permissions import CORE_MODULE_KEYS, MODULES_BY_KEY, module_group_tree
from app.core.tenancy import invalidate_tenant_cache
from app.db.mongo import C, collection
from app.models.base import AppModel, Msg, serialize_doc, utcnow
from app.utils.audit import record

router = APIRouter(prefix="/settings", tags=["Settings"])

Reader = Annotated[AuthContext, Depends(require("settings:read"))]
Editor = Annotated[AuthContext, Depends(require("settings:update"))]
Owner = Annotated[AuthContext, Depends(require_owner)]


class ProfileRequest(AppModel):
    name: str | None = None
    legal_name: str | None = None
    institution_type: str | None = None
    website: str | None = None
    affiliation_board: str | None = None
    registration_number: str | None = None
    established_year: int | None = None
    contact: dict | None = None
    address: dict | None = None


class LocalisationRequest(AppModel):
    timezone: str | None = None
    currency: str | None = None
    locale: str | None = None
    academic_year_start_month: int | None = None


class BrandingRequest(AppModel):
    logo: dict | None = None
    logo_dark: dict | None = None
    favicon: dict | None = None
    login_banner: dict | None = None
    primary_color: str | None = None
    accent_color: str | None = None
    tagline: str | None = None


class ModulesRequest(AppModel):
    enabled_modules: list[str]


class PreferencesRequest(AppModel):
    """Free-form institution preferences — numbering formats, grading policy,
    attendance rules. Stored under ``settings`` so a school can shape the
    product without us shipping a release."""

    values: dict[str, Any]


@router.get("", summary="Everything on the settings screen")
async def get_settings(auth: Reader, tenant: TenantDep):
    doc = await collection(C.TENANTS).find_one({"_id": tenant.id})
    out = serialize_doc(doc) or {}
    out.pop("notes", None)
    if not auth.is_owner:
        out.pop("license_key", None)
    groups = module_group_tree(tenant.institution_type)
    for group in groups:
        for module in group["modules"]:
            module["enabled"] = tenant.module_enabled(module["key"])
            module["locked"] = (
                not module["optional"]
                or (app_settings.is_saas and not tenant.is_dedicated
                    and module["key"] not in (doc or {}).get("enabled_modules", []))
            )
    return {
        "institution": out,
        "deployment_mode": app_settings.deployment_mode,
        "is_dedicated": tenant.is_dedicated,
        "modules": groups,
        "can_manage_modules": tenant.is_dedicated or auth.is_owner,
    }


@router.patch("/profile", summary="Update institution details")
async def update_profile(
    payload: ProfileRequest, auth: Editor, tenant: TenantDep, request: Request
):
    return await _apply(tenant, auth, payload.model_dump(exclude_none=True),
                        "settings.profile", request)


@router.patch("/localisation", summary="Timezone, currency and academic calendar")
async def update_localisation(
    payload: LocalisationRequest, auth: Editor, tenant: TenantDep, request: Request
):
    data = payload.model_dump(exclude_none=True)
    month = data.get("academic_year_start_month")
    if month is not None and not 1 <= month <= 12:
        raise ValidationError("Academic year start month must be between 1 and 12")
    return await _apply(tenant, auth, data, "settings.localisation", request)


@router.patch("/branding", summary="Logo, colours and login banner")
async def update_branding(
    payload: BrandingRequest, auth: Editor, tenant: TenantDep, request: Request
):
    data = {f"branding.{k}": v for k, v in payload.model_dump(exclude_none=True).items()}
    return await _apply(tenant, auth, data, "settings.branding", request)


@router.patch("/preferences", summary="Numbering, grading and policy preferences")
async def update_preferences(
    payload: PreferencesRequest, auth: Editor, tenant: TenantDep, request: Request
):
    data = {f"settings.{k}": v for k, v in payload.values.items()}
    return await _apply(tenant, auth, data, "settings.preferences", request)


@router.put("/modules", summary="Switch modules on or off")
async def update_modules(
    payload: ModulesRequest, auth: Owner, tenant: TenantDep, request: Request
):
    """A dedicated institution controls its own module list outright.

    On a shared subscription the plan is the ceiling: a school can switch a
    module *off*, but cannot switch on something it has not bought.
    """
    requested = set(payload.enabled_modules) | set(CORE_MODULE_KEYS)
    unknown = [m for m in requested if m not in MODULES_BY_KEY]
    if unknown:
        raise ValidationError(f"Unknown module(s): {', '.join(unknown[:5])}")

    if not tenant.is_dedicated:
        plan = await collection(C.PLANS).find_one({"key": tenant.plan_key})
        allowed = set((plan or {}).get("included_modules") or tenant.enabled_modules)
        beyond = sorted(requested - allowed - set(CORE_MODULE_KEYS))
        if beyond:
            names = ", ".join(MODULES_BY_KEY[m].label for m in beyond[:4])
            raise Forbidden(
                f"Your plan does not include {names}. Upgrade to switch these on."
            )
    return await _apply(tenant, auth, {"enabled_modules": sorted(requested)},
                        "settings.modules", request)


@router.get("/license", summary="Licence details (owner only)")
async def license_info(auth: Owner, tenant: TenantDep):
    doc = await collection(C.TENANTS).find_one({"_id": tenant.id})
    return {
        "deployment": tenant.deployment,
        "plan_key": tenant.plan_key,
        "license_key": (doc or {}).get("license_key", ""),
        "licensed_at": (doc or {}).get("licensed_at"),
        "subscription_status": tenant.subscription_status,
        "subscription_valid_till": (
            tenant.subscription_valid_till.isoformat()
            if tenant.subscription_valid_till else None
        ),
        "limits": {
            "max_students": tenant.limits.max_students,
            "max_staff": tenant.limits.max_staff,
            "max_storage_mb": tenant.limits.max_storage_mb,
            "max_admin_users": tenant.limits.max_admin_users,
        },
        "note": (
            "This institution owns its deployment outright. No plan limits apply."
            if tenant.is_dedicated
            else "This institution is on a shared subscription."
        ),
    }


async def _apply(
    tenant, auth: AuthContext, data: dict, action: str, request: Request
) -> dict:
    if not data:
        raise ValidationError("Nothing to update")
    doc = await collection(C.TENANTS).find_one_and_update(
        {"_id": tenant.id}, {"$set": {**data, "updated_at": utcnow()}}, return_document=True
    )
    invalidate_tenant_cache()
    await record(auth, action, entity_type="tenant", entity_id=tenant.id,
                 changes={k: v for k, v in data.items() if not isinstance(v, dict)},
                 request=request)
    return serialize_doc(doc) or {}


@router.get("/export/summary", summary="What a data export would contain")
async def export_summary(auth: Owner, tenant: TenantDep):
    from app.modules.settings.backup import export_summary as summarise

    return await summarise(tenant)


@router.get("/export", summary="Download every record this institution owns")
async def export_data(auth: Owner, tenant: TenantDep, request: Request):
    """One zip, one JSON array per collection, in MongoDB Extended JSON.

    Ids are preserved, so this restores into a dedicated deployment unchanged —
    which is what makes "take our data and run it ourselves" a real option
    rather than a promise."""
    from fastapi import Response

    from app.modules.settings.backup import export_tenant

    content, filename, counts = await export_tenant(tenant)
    await record(auth, "settings.data_exported", entity_type="tenant", entity_id=tenant.id,
                 changes={"documents": sum(counts.values())}, request=request)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/reset-cache", response_model=Msg, summary="Refresh cached institution settings")
async def reset_cache(auth: Editor, tenant: TenantDep):
    invalidate_tenant_cache()
    return Msg(detail="Settings cache cleared")
