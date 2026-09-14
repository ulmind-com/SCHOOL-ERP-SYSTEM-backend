"""Exam operations that are more than a row write: marking schemes, re-grading."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.context import AuthContext
from app.core.deps import TenantDep, require
from app.core.exceptions import ValidationError
from app.models.base import AppModel
from app.modules.exams import service
from app.utils.audit import record

router = APIRouter(prefix="/exams", tags=["Exams"])

Reader = Annotated[AuthContext, Depends(require("exams:read"))]
Setter = Annotated[AuthContext, Depends(require("exams:update"))]


class MarkingSchemeRequest(AppModel):
    max_marks: float
    pass_marks: float


@router.get("/{exam_id}/scheme/{subject_id}",
            summary="What a subject is marked out of in this exam")
async def get_scheme(exam_id: str, subject_id: str, auth: Reader, tenant: TenantDep):
    return await service.marking_scheme(tenant, exam_id, subject_id)


@router.put("/{exam_id}/scheme/{subject_id}", summary="Set the marking scheme")
async def put_scheme(
    exam_id: str,
    subject_id: str,
    payload: MarkingSchemeRequest,
    auth: Setter,
    tenant: TenantDep,
    request: Request,
):
    """Marks already entered are recalculated against the new figures, so a
    correction here cannot leave stale percentages on a report card."""
    if payload.max_marks <= 0:
        raise ValidationError("Maximum marks must be greater than zero")
    if payload.pass_marks < 0 or payload.pass_marks > payload.max_marks:
        raise ValidationError("The pass mark has to sit between zero and the maximum")

    result = await service.set_marking_scheme(
        tenant, auth,
        exam_id=exam_id, subject_id=subject_id,
        max_marks=payload.max_marks, pass_marks=payload.pass_marks,
    )
    await record(auth, "exams.scheme_set", entity_type="exams", entity_id=exam_id,
                 changes={"max_marks": payload.max_marks,
                          "pass_marks": payload.pass_marks,
                          "regraded": result["regraded"]}, request=request)
    return result


@router.post("/{exam_id}/regrade", summary="Recalculate every mark in this exam")
async def regrade(exam_id: str, auth: Setter, tenant: TenantDep, request: Request):
    """Run this after changing a grade scale — the bands are read at marking
    time, so existing marks keep the grade they were given until told otherwise."""
    changed = await service.regrade(tenant, exam_id=exam_id)
    await record(auth, "exams.regrade", entity_type="exams", entity_id=exam_id,
                 changes={"regraded": changed}, request=request)
    return {"regraded": changed, "detail": f"{changed} mark(s) recalculated"}
