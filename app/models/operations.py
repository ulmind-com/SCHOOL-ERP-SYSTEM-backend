"""Attendance, assignments, examinations, results and learning material."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, computed_field

from app.models.base import AppModel, FileRef, PyObjectId, TenantDocument


class AttendanceStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    LATE = "late"
    HALF_DAY = "half_day"
    EXCUSED = "excused"
    HOLIDAY = "holiday"
    LEAVE = "leave"


PRESENT_LIKE = {AttendanceStatus.PRESENT, AttendanceStatus.LATE, AttendanceStatus.HALF_DAY}


class AttendanceSession(TenantDocument):
    """One register: a section on a date, for a period or for the whole day."""

    section_id: PyObjectId
    class_id: PyObjectId | None = None
    date: datetime
    session_key: str = "day"        # "day" | "p1" | subject id — keeps the unique index honest
    subject_id: PyObjectId | None = None
    period_id: PyObjectId | None = None
    academic_year_id: PyObjectId | None = None
    taken_by: PyObjectId | None = None
    taken_at: datetime | None = None
    total: int = 0
    present: int = 0
    absent: int = 0
    late: int = 0
    on_leave: int = 0
    is_locked: bool = False
    notes: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def percentage(self) -> float:
        return round(self.present / self.total * 100, 2) if self.total else 0.0


class AttendanceRecord(TenantDocument):
    student_id: PyObjectId
    section_id: PyObjectId | None = None
    class_id: PyObjectId | None = None
    date: datetime
    session_key: str = "day"
    subject_id: PyObjectId | None = None
    status: AttendanceStatus = AttendanceStatus.PRESENT
    in_time: str = ""
    out_time: str = ""
    minutes_late: int = 0
    remark: str = ""
    marked_by: PyObjectId | None = None
    academic_year_id: PyObjectId | None = None


class Assignment(TenantDocument):
    title: str
    description: str = ""
    subject_id: PyObjectId | None = None
    section_ids: list[PyObjectId] = Field(default_factory=list)
    class_id: PyObjectId | None = None
    academic_year_id: PyObjectId | None = None
    assigned_by: PyObjectId | None = None
    assigned_on: datetime | None = None
    due_date: datetime | None = None
    max_marks: float = 0
    type: str = "homework"          # homework | project | lab | reading | presentation
    attachments: list[FileRef] = Field(default_factory=list)
    #: How the work comes back. ``online`` means students hand it in through the
    #: portal; ``offline`` means the teacher collects it in class and ticks off
    #: who handed it in. The two produce the same submission rows, so grading,
    #: counts and report cards do not care which was used.
    submission_mode: str = "online"  # online | offline
    allow_late_submission: bool = True
    status: str = "draft"           # draft | published | closed
    published_at: datetime | None = None
    submission_count: int = 0
    graded_count: int = 0


class AssignmentSubmission(TenantDocument):
    assignment_id: PyObjectId
    student_id: PyObjectId
    submitted_at: datetime | None = None
    status: str = "pending"         # pending | submitted | late | graded | resubmit
    text_answer: str = ""
    attachments: list[FileRef] = Field(default_factory=list)
    #: True when a teacher recorded a hand-in that happened on paper. The row
    #: then carries who ticked it, because nobody else can vouch for it.
    collected_offline: bool = False
    collected_by: PyObjectId | None = None
    marks: float | None = None
    grade: str = ""
    feedback: str = ""
    graded_by: PyObjectId | None = None
    graded_at: datetime | None = None


class ExamType(StrEnum):
    UNIT_TEST = "unit_test"
    MIDTERM = "midterm"
    FINAL = "final"
    PRACTICAL = "practical"
    ASSIGNMENT = "assignment"
    INTERNAL = "internal"
    BOARD = "board"


class Exam(TenantDocument):
    name: str                       # "Half Yearly 2026"
    type: ExamType = ExamType.UNIT_TEST
    academic_year_id: PyObjectId | None = None
    term_id: PyObjectId | None = None
    class_ids: list[PyObjectId] = Field(default_factory=list)
    start_date: datetime | None = None
    end_date: datetime | None = None
    grade_scale_id: PyObjectId | None = None
    weightage_percent: float = 100
    status: str = "scheduled"       # scheduled | ongoing | marks_entry | completed | published
    instructions: str = ""
    result_published_at: datetime | None = None


class ExamSchedule(TenantDocument):
    exam_id: PyObjectId
    subject_id: PyObjectId
    class_id: PyObjectId | None = None
    section_ids: list[PyObjectId] = Field(default_factory=list)
    date: datetime
    start_time: str = ""
    end_time: str = ""
    room: str = ""
    max_marks: float = 100
    pass_marks: float = 33
    invigilator_ids: list[PyObjectId] = Field(default_factory=list)


class Mark(TenantDocument):
    exam_id: PyObjectId
    student_id: PyObjectId
    subject_id: PyObjectId
    section_id: PyObjectId | None = None
    class_id: PyObjectId | None = None
    max_marks: float = 100
    marks_obtained: float | None = None
    practical_marks: float | None = None
    internal_marks: float | None = None
    total_marks: float | None = None
    percentage: float | None = None
    grade: str = ""
    grade_points: float | None = None
    is_absent: bool = False
    is_pass: bool | None = None
    remark: str = ""
    entered_by: PyObjectId | None = None
    entered_at: datetime | None = None


class SubjectResult(AppModel):
    subject_id: str
    subject_name: str = ""
    max_marks: float = 100
    marks_obtained: float = 0
    grade: str = ""
    grade_points: float = 0
    is_pass: bool = True
    remark: str = ""


class ReportCard(TenantDocument):
    student_id: PyObjectId
    exam_id: PyObjectId
    academic_year_id: PyObjectId | None = None
    class_id: PyObjectId | None = None
    section_id: PyObjectId | None = None
    subjects: list[SubjectResult] = Field(default_factory=list)
    total_max_marks: float = 0
    total_obtained: float = 0
    percentage: float = 0
    grade: str = ""
    gpa: float | None = None
    rank_in_section: int | None = None
    rank_in_class: int | None = None
    result: str = "pending"         # pass | fail | pending
    attendance_percentage: float | None = None
    class_teacher_remark: str = ""
    principal_remark: str = ""
    published_at: datetime | None = None
    published_by: PyObjectId | None = None
    pdf: FileRef | None = None


class LearningMaterial(TenantDocument):
    title: str
    description: str = ""
    subject_id: PyObjectId | None = None
    class_id: PyObjectId | None = None
    section_ids: list[PyObjectId] = Field(default_factory=list)
    syllabus_unit_id: PyObjectId | None = None
    type: str = "document"          # document | video | link | slide | quiz
    files: list[FileRef] = Field(default_factory=list)
    external_url: str = ""
    uploaded_by: PyObjectId | None = None
    is_published: bool = False
    published_at: datetime | None = None
    view_count: int = 0
    tags: list[str] = Field(default_factory=list)


class Certificate(TenantDocument):
    serial_number: str
    student_id: PyObjectId | None = None
    staff_id: PyObjectId | None = None
    type: str = "bonafide"          # bonafide | transfer | character | completion | id_card
    title: str = ""
    issued_on: datetime | None = None
    issued_by: PyObjectId | None = None
    valid_till: datetime | None = None
    content: str = ""
    fields: dict = Field(default_factory=dict)
    pdf: FileRef | None = None
    status: str = "issued"          # draft | issued | revoked


def percentage_of(obtained: float | None, maximum: float | None) -> float | None:
    if obtained is None or not maximum:
        return None
    return round(obtained / maximum * 100, 2)
