"""Payroll processing endpoints."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from app.core.context import AuthContext
from app.core.deps import TenantDep, require
from app.models.base import AppModel
from app.modules.payroll import service
from app.utils.audit import record

router = APIRouter(prefix="/payroll", tags=["Payroll"])

Reader = Annotated[AuthContext, Depends(require("payroll:read"))]
Processor = Annotated[AuthContext, Depends(require("payroll:create"))]
Approver = Annotated[AuthContext, Depends(require("payroll:approve"))]


class GenerateRequest(AppModel):
    year: int
    month: int
    staff_ids: list[str] | None = None


@router.post("/generate", summary="Generate payslips for a month")
async def generate(
    payload: GenerateRequest, auth: Processor, tenant: TenantDep, request: Request
):
    """Draws on each person's salary structure and their attendance for the
    month. Re-running a draft replaces its payslips; an approved run is frozen."""
    result = await service.generate(
        tenant, auth, year=payload.year, month=payload.month, staff_ids=payload.staff_ids
    )
    await record(auth, "payroll.generated", entity_type="payroll_run",
                 entity_id=result["run_id"], entity_label=result["period"],
                 changes={"generated": result["generated"], "net": result["net_total"]},
                 request=request)
    return result


# Note: not "/runs/{run_id}" — the registry already owns that path as plain
# CRUD over payroll_runs, and whichever router mounts first would shadow the
# other. This sits one level deeper so both can coexist.
@router.get("/runs/{run_id}/payslips", summary="A run with its payslips")
async def run_detail(run_id: str, auth: Reader, tenant: TenantDep):
    return await service.run_detail(tenant, run_id)


@router.post("/runs/{run_id}/approve", summary="Approve and freeze a run")
async def approve(run_id: str, auth: Approver, tenant: TenantDep, request: Request):
    result = await service.approve(tenant, auth, run_id)
    await record(auth, "payroll.approved", entity_type="payroll_run", entity_id=run_id,
                 request=request)
    return result


@router.post("/runs/{run_id}/mark-paid", summary="Mark salaries as paid")
async def mark_paid(
    run_id: str,
    auth: Approver,
    tenant: TenantDep,
    request: Request,
    reference: Annotated[str, Body(embed=True)] = "",
):
    result = await service.mark_paid(tenant, auth, run_id, reference=reference)
    await record(auth, "payroll.paid", entity_type="payroll_run", entity_id=run_id,
                 request=request)
    return result
