"""Attendance — register taking, summaries and defaulter lists."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.context import AuthContext
from app.core.deps import TenantDep, require
from app.models.base import AppModel
from app.modules.attendance import service
from app.utils.audit import record

router = APIRouter(prefix="/attendance", tags=["Attendance"])

Reader = Annotated[AuthContext, Depends(require("attendance:read"))]
Taker = Annotated[AuthContext, Depends(require("attendance:create"))]
Admin = Annotated[AuthContext, Depends(require("attendance:delete"))]


class AttendanceEntry(AppModel):
    student_id: str
    status: str = "present"
    remark: str = ""
    in_time: str = ""
    minutes_late: int = 0


class TakeRegisterRequest(AppModel):
    section_id: str
    date: date
    entries: list[AttendanceEntry]
    session_key: str = "day"
    subject_id: str | None = None
    period_id: str | None = None
    notes: str = ""


class LockRequest(AppModel):
    section_id: str
    date: date
    session_key: str = "day"
    locked: bool = True


@router.get("/register", summary="Open a register (roster + anything already marked)")
async def get_register(
    auth: Reader,
    tenant: TenantDep,
    section_id: str,
    on: Annotated[date | None, Query(alias="date")] = None,
    session_key: str = "day",
):
    from app.core.scoping import family_student_ids

    return await service.get_register(
        tenant, section_id=section_id, on=on or date.today(), session_key=session_key,
        only_students=await family_student_ids(auth, tenant),
    )


@router.post("/register", summary="Save a register")
async def take_register(
    payload: TakeRegisterRequest, auth: Taker, tenant: TenantDep, request: Request
):
    result = await service.take_register(
        tenant, auth,
        section_id=payload.section_id,
        on=payload.date,
        entries=[e.model_dump() for e in payload.entries],
        session_key=payload.session_key,
        subject_id=payload.subject_id,
        period_id=payload.period_id,
        notes=payload.notes,
    )
    await record(auth, "attendance.taken", entity_type="attendance",
                 entity_label=f"{payload.section_id} {payload.date}",
                 changes={"counts": result["counts"]}, request=request)
    return result


@router.get("/summary", summary="Attendance over a date range")
async def summary(
    auth: Reader,
    tenant: TenantDep,
    start: date,
    end: date,
    section_id: str | None = None,
    class_id: str | None = None,
):
    return await service.section_summary(
        tenant, section_id=section_id, class_id=class_id, start=start, end=end
    )


@router.get("/student/{student_id}/calendar", summary="One student's month")
async def calendar(
    student_id: str, auth: Reader, tenant: TenantDep,
    year: int | None = None, month: int | None = None,
):
    today = date.today()
    return await service.student_calendar(
        tenant, student_id, year=year or today.year, month=month or today.month
    )


@router.get("/defaulters", summary="Students below an attendance threshold")
async def defaulters(
    auth: Reader,
    tenant: TenantDep,
    threshold: Annotated[float, Query(ge=0, le=100)] = 75,
    class_id: str | None = None,
    section_id: str | None = None,
    start: date | None = None,
    end: date | None = None,
):
    return await service.defaulters(
        tenant, threshold=threshold, class_id=class_id, section_id=section_id,
        start=start, end=end,
    )


@router.post("/lock", summary="Lock or reopen a register")
async def lock(payload: LockRequest, auth: Admin, tenant: TenantDep, request: Request):
    result = await service.set_lock(
        tenant, section_id=payload.section_id, on=payload.date,
        session_key=payload.session_key, locked=payload.locked,
    )
    await record(auth, "attendance.lock", entity_type="attendance",
                 changes={"locked": payload.locked}, request=request)
    return result
