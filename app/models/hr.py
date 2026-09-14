"""Leave, staff attendance and appraisals."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.base import FileRef, PyObjectId, TenantDocument


class LeaveType(TenantDocument):
    code: str
    name: str
    annual_quota: float = 0
    is_paid: bool = True
    carry_forward: bool = False
    max_carry_forward: float = 0
    requires_document: bool = False
    applies_to: str = "all"         # all | teaching | non_teaching
    is_active: bool = True


class LeaveRequest(TenantDocument):
    staff_id: PyObjectId
    staff_name: str = ""
    leave_type_id: PyObjectId | None = None
    leave_type_name: str = ""
    from_date: datetime
    to_date: datetime
    days: float = 1
    is_half_day: bool = False
    reason: str = ""
    status: str = "pending"         # pending | approved | rejected | cancelled
    applied_at: datetime | None = None
    reviewed_by: PyObjectId | None = None
    reviewed_at: datetime | None = None
    review_note: str = ""
    substitute_staff_id: PyObjectId | None = None
    attachments: list[FileRef] = Field(default_factory=list)


class StaffAttendance(TenantDocument):
    staff_id: PyObjectId
    date: datetime
    status: str = "present"         # present | absent | late | half_day | leave | holiday | wfh
    in_time: str = ""
    out_time: str = ""
    worked_hours: float = 0
    minutes_late: int = 0
    leave_request_id: PyObjectId | None = None
    marked_by: PyObjectId | None = None
    source: str = "manual"          # manual | biometric | import
    remark: str = ""


class Appraisal(TenantDocument):
    staff_id: PyObjectId
    period: str = ""                # "2026-27"
    reviewer_id: PyObjectId | None = None
    criteria: list[dict] = Field(default_factory=list)
    overall_score: float | None = None
    overall_rating: str = ""
    strengths: str = ""
    improvements: str = ""
    goals: list[dict] = Field(default_factory=list)
    staff_comment: str = ""
    status: str = "draft"           # draft | submitted | reviewed | acknowledged
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
