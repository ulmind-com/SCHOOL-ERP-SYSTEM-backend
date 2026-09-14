"""Report catalogue, generation and export."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.context import AuthContext
from app.core.deps import TenantDep, require
from app.core.exceptions import Forbidden
from app.core.permissions import has_permission
from app.modules.reports import service
from app.utils.audit import record

router = APIRouter(prefix="/reports", tags=["Reports"])

Viewer = Annotated[AuthContext, Depends(require("reports:read"))]
Exporter = Annotated[AuthContext, Depends(require("reports:export"))]


def _params(
    start: date | None, end: date | None, class_id: str, section_id: str,
    exam_id: str, department_id: str, status: str, threshold: float | None,
) -> dict:
    return {
        k: v
        for k, v in {
            "start": start, "end": end, "class_id": class_id, "section_id": section_id,
            "exam_id": exam_id, "department_id": department_id, "status": status,
            "threshold": threshold,
        }.items()
        if v not in (None, "")
    }


def _guard(auth: AuthContext, tenant, key: str) -> None:
    """A report may not be a way around the permission on its own module."""
    entry = next((r for r in service.REPORT_CATALOGUE if r["key"] == key), None)
    if entry is None:
        return
    if not tenant.module_enabled(entry["module"]):
        raise Forbidden(f"The {entry['module'].replace('_', ' ')} module is not enabled")
    if not has_permission(auth.permissions, f"{entry['module']}:read"):
        raise Forbidden("You do not have access to the data behind this report")


@router.get("", summary="Reports you can run")
async def catalogue(auth: Viewer, tenant: TenantDep):
    reports = service.catalogue_for(tenant, auth.permissions)
    groups: dict[str, list] = {}
    for report in reports:
        groups.setdefault(report["group"], []).append(report)
    return {
        "groups": [{"group": g, "reports": r} for g, r in groups.items()],
        "count": len(reports),
    }


@router.get("/{key}", summary="Run a report")
async def run(
    key: str,
    auth: Viewer,
    tenant: TenantDep,
    start: date | None = None,
    end: date | None = None,
    class_id: str = "",
    section_id: str = "",
    exam_id: str = "",
    department_id: str = "",
    status: str = "",
    threshold: Annotated[float | None, Query(ge=0, le=100)] = None,
):
    _guard(auth, tenant, key)
    report = await service.build(
        tenant, key,
        **_params(start, end, class_id, section_id, exam_id, department_id, status, threshold),
    )
    return report.as_dict()


@router.get("/{key}/export.csv", summary="Export a report as CSV")
async def export_csv(
    key: str,
    auth: Exporter,
    tenant: TenantDep,
    request: Request,
    start: date | None = None,
    end: date | None = None,
    class_id: str = "",
    section_id: str = "",
    exam_id: str = "",
    department_id: str = "",
    status: str = "",
    threshold: Annotated[float | None, Query(ge=0, le=100)] = None,
):
    _guard(auth, tenant, key)
    report = await service.build(
        tenant, key,
        **_params(start, end, class_id, section_id, exam_id, department_id, status, threshold),
    )
    await record(auth, "reports.export", entity_type="report", entity_label=key,
                 changes={"format": "csv", "rows": len(report.rows)}, request=request)
    return Response(
        content=service.to_csv(report),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{key}.csv"'},
    )


@router.get("/{key}/export.xlsx", summary="Export a report as Excel")
async def export_xlsx(
    key: str,
    auth: Exporter,
    tenant: TenantDep,
    request: Request,
    start: date | None = None,
    end: date | None = None,
    class_id: str = "",
    section_id: str = "",
    exam_id: str = "",
    department_id: str = "",
    status: str = "",
    threshold: Annotated[float | None, Query(ge=0, le=100)] = None,
):
    _guard(auth, tenant, key)
    report = await service.build(
        tenant, key,
        **_params(start, end, class_id, section_id, exam_id, department_id, status, threshold),
    )
    await record(auth, "reports.export", entity_type="report", entity_label=key,
                 changes={"format": "xlsx", "rows": len(report.rows)}, request=request)
    return Response(
        content=service.to_xlsx(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{key}.xlsx"'},
    )
