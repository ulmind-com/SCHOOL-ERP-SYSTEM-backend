"""Online classes and homework submission endpoints."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.context import AuthContext
from app.core.deps import CurrentUser, TenantDep, require
from app.models.base import AppModel
from app.modules.lms import service
from app.utils.audit import record

router = APIRouter(prefix="/live-classes", tags=["Online Classes"])

Host = Annotated[AuthContext, Depends(require("lms:create"))]
Manager = Annotated[AuthContext, Depends(require("lms:update"))]


class ScheduleRequest(AppModel):
    title: str
    section_ids: list[str]
    starts_at: datetime
    duration_minutes: int = 45
    subject_id: str | None = None
    meeting_url: str
    meeting_id: str = ""
    passcode: str = ""
    platform: str = "other"        # zoom | meet | teams | other
    description: str = ""


@router.get("", summary="Classes you can attend or host")
async def upcoming(
    auth: CurrentUser, tenant: TenantDep, days: Annotated[int, Query(ge=1, le=60)] = 7
):
    """A link is withheld until fifteen minutes before the class and expires
    three hours after it ends."""
    return await service.upcoming_classes(tenant, auth, days=days)


@router.post("", status_code=201, summary="Schedule a class")
async def schedule(payload: ScheduleRequest, auth: Host, tenant: TenantDep, request: Request):
    result = await service.schedule_class(tenant, auth, **payload.model_dump())
    await record(auth, "lms.class_scheduled", entity_type="online_class",
                 entity_id=result.get("id"), entity_label=payload.title, request=request)
    return result


@router.post("/{class_id}/join", summary="Get the joining link")
async def join(class_id: str, auth: CurrentUser, tenant: TenantDep):
    return await service.join(tenant, auth, class_id)


@router.post("/{class_id}/cancel", summary="Cancel a class")
async def cancel(
    class_id: str,
    auth: Manager,
    tenant: TenantDep,
    request: Request,
    reason: str = "",
):
    result = await service.cancel_class(tenant, auth, class_id, reason)
    await record(auth, "lms.class_cancelled", entity_type="online_class",
                 entity_id=class_id, request=request)
    return result


# ── Homework ──────────────────────────────────────────────────────────────
homework_router = APIRouter(prefix="/homework", tags=["Assignments"])

Grader = Annotated[AuthContext, Depends(require("assignments:update"))]


class SubmitRequest(AppModel):
    assignment_id: str
    text_answer: str = ""
    attachments: list[dict] = []


class GradeRequest(AppModel):
    marks: float | None = None
    grade: str = ""
    feedback: str = ""


class CollectRequest(AppModel):
    #: The whole roster's answer, not a diff — unticking someone un-collects
    #: them, the way a register works.
    received: list[str] = []


@homework_router.get("/mine", summary="A student's own homework")
async def mine(auth: CurrentUser, tenant: TenantDep):
    return await service.my_assignments(tenant, auth)


@homework_router.post("/submit", status_code=201, summary="Submit homework")
async def submit(payload: SubmitRequest, auth: CurrentUser, tenant: TenantDep):
    return await service.submit_homework(
        tenant, auth,
        assignment_id=payload.assignment_id,
        text_answer=payload.text_answer,
        attachments=payload.attachments,
    )


@homework_router.get("/assignments/{assignment_id}", summary="Who has submitted")
async def submissions(
    assignment_id: str,
    auth: Annotated[AuthContext, Depends(require("assignments:read"))],
    tenant: TenantDep,
):
    """Lists every student who *should* submit, so the gaps are visible."""
    return await service.assignment_submissions(tenant, assignment_id)


@homework_router.post("/assignments/{assignment_id}/publish", summary="Publish and notify")
async def publish(
    assignment_id: str,
    auth: Annotated[AuthContext, Depends(require("assignments:publish"))],
    tenant: TenantDep,
    request: Request,
):
    result = await service.publish_assignment(tenant, auth, assignment_id)
    await record(auth, "assignments.published", entity_type="assignments",
                 entity_id=assignment_id, entity_label=result.get("title", ""),
                 request=request)
    return result


@homework_router.post("/assignments/{assignment_id}/close", summary="Stop accepting it")
async def close(
    assignment_id: str,
    auth: Annotated[AuthContext, Depends(require("assignments:publish"))],
    tenant: TenantDep,
    request: Request,
):
    result = await service.close_assignment(tenant, auth, assignment_id)
    await record(auth, "assignments.closed", entity_type="assignments",
                 entity_id=assignment_id, request=request)
    return result


@homework_router.post("/assignments/{assignment_id}/collect",
                      summary="Record who handed it in on paper")
async def collect(
    assignment_id: str,
    payload: CollectRequest,
    auth: Grader,
    tenant: TenantDep,
    request: Request,
):
    """For assignments the teacher collects in class rather than through the
    portal. Send the full roster's answer; this screen is the record."""
    result = await service.collect_offline(
        tenant, auth, assignment_id, received=payload.received
    )
    await record(auth, "assignments.collected", entity_type="assignments",
                 entity_id=assignment_id,
                 changes={"received": result["received"], "expected": result["expected"]},
                 request=request)
    return result


@homework_router.post("/submissions/{submission_id}/grade", summary="Grade a submission")
async def grade(
    submission_id: str, payload: GradeRequest, auth: Grader, tenant: TenantDep,
    request: Request,
):
    result = await service.grade_submission(
        tenant, auth, submission_id,
        marks=payload.marks, grade=payload.grade, feedback=payload.feedback,
    )
    await record(auth, "assignments.graded", entity_type="assignment_submission",
                 entity_id=submission_id, changes={"marks": payload.marks}, request=request)
    return result
