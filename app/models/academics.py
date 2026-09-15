"""Academic structure.

Deliberately shaped to fit schools *and* higher education without two schemas:
a school uses Class → Section → Subject; a college uses Department → Program →
Class(semester) → Section → Subject. The extra levels are optional, so a school
simply leaves them empty.
"""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum

from pydantic import Field

from app.models.base import AppModel, FileRef, PyObjectId, TenantDocument


class TermType(StrEnum):
    SEMESTER = "semester"
    TRIMESTER = "trimester"
    QUARTER = "quarter"
    TERM = "term"


class AcademicYear(TenantDocument):
    name: str                      # "2026-27"
    start_date: datetime
    end_date: datetime
    is_current: bool = False
    status: str = "active"         # upcoming | active | closed
    description: str = ""


class Holiday(TenantDocument):
    """A day the institution is closed — or open but not teaching.

    Kept apart from Events because the attendance register has to consult it on
    every open, and an event is a notice board entry that may or may not mean
    anything to the register.
    """

    name: str                       # "Durga Puja", "Independence Day"
    start_date: datetime
    #: Inclusive. ``None`` means the holiday is one day long.
    end_date: datetime | None = None
    type: str = "public"            # public | festival | vacation | exam_break | other
    description: str = ""
    #: Whether a register is still taken. A celebrated holiday happens *at*
    #: school — Independence Day, sports day, a founder's day — so the children
    #: are present and their attendance counts. Off means the school is shut.
    attendance_required: bool = False
    #: Empty means the whole institution. A class that has an exam through the
    #: break can be left out of it.
    class_ids: list[PyObjectId] = Field(default_factory=list)
    academic_year_id: PyObjectId | None = None
    is_active: bool = True

    @property
    def last_date(self) -> datetime:
        return self.end_date or self.start_date


class Term(TenantDocument):
    academic_year_id: PyObjectId
    name: str                      # "Semester 1", "Term 2"
    type: TermType = TermType.TERM
    start_date: datetime
    end_date: datetime
    order: int = 1
    is_current: bool = False


class Department(TenantDocument):
    """Faculty / department. Optional for schools."""

    code: str
    name: str
    description: str = ""
    head_staff_id: PyObjectId | None = None
    email: str = ""
    phone: str = ""
    is_active: bool = True


class Program(TenantDocument):
    """A degree or course of study — B.Tech CSE, B.Com Hons. Colleges only."""

    code: str
    name: str
    department_id: PyObjectId | None = None
    level: str = "undergraduate"   # certificate | diploma | undergraduate | postgraduate | doctoral
    duration_years: float = 3
    total_semesters: int = 6
    total_credits: float = 0
    description: str = ""
    is_active: bool = True


class SchoolClass(TenantDocument):
    """A year group: 'Class 8' at a school, 'Semester 3' at a college."""

    name: str
    numeric_level: int | None = None       # 8 -> sortable
    academic_year_id: PyObjectId
    program_id: PyObjectId | None = None
    department_id: PyObjectId | None = None
    stream: str = ""                       # Science / Commerce / Arts
    semester: int | None = None
    capacity: int = 0
    class_teacher_id: PyObjectId | None = None
    fee_structure_id: PyObjectId | None = None
    is_active: bool = True
    order: int = 0


class Section(TenantDocument):
    """A teachable group inside a class — 8A, 8B."""

    class_id: PyObjectId
    name: str                              # "A"
    academic_year_id: PyObjectId
    class_teacher_id: PyObjectId | None = None
    room: str = ""
    capacity: int = 40
    current_strength: int = 0
    is_active: bool = True


class Subject(TenantDocument):
    code: str
    name: str
    short_name: str = ""
    class_id: PyObjectId | None = None
    department_id: PyObjectId | None = None
    program_id: PyObjectId | None = None
    type: str = "core"                     # core | elective | optional | practical | project
    credits: float = 0
    max_marks: float = 100
    pass_marks: float = 33
    has_practical: bool = False
    practical_max_marks: float = 0
    is_graded: bool = True                 # False for non-scholastic subjects
    description: str = ""
    is_active: bool = True
    order: int = 0


class SubjectAssignment(TenantDocument):
    """Who teaches which subject to which section."""

    subject_id: PyObjectId
    section_id: PyObjectId
    class_id: PyObjectId | None = None
    staff_id: PyObjectId
    academic_year_id: PyObjectId
    is_primary: bool = True


class Period(TenantDocument):
    """A slot in the daily bell schedule."""

    name: str
    start_time: str                        # "09:00"
    end_time: str
    order: int = 1
    is_break: bool = False
    academic_year_id: PyObjectId | None = None


class TimetableSlot(TenantDocument):
    academic_year_id: PyObjectId
    section_id: PyObjectId
    class_id: PyObjectId | None = None
    day_of_week: int = 1                   # 1 = Monday … 7 = Sunday
    period_id: PyObjectId | None = None
    period_name: str = ""
    start_time: str = ""
    end_time: str = ""
    subject_id: PyObjectId | None = None
    staff_id: PyObjectId | None = None
    room: str = ""
    type: str = "lecture"                  # lecture | lab | tutorial | break | activity
    is_published: bool = False


class SyllabusUnit(TenantDocument):
    subject_id: PyObjectId
    class_id: PyObjectId | None = None
    title: str
    description: str = ""
    order: int = 1
    planned_hours: float = 0
    completed: bool = False
    completed_on: datetime | None = None
    topics: list[dict] = Field(default_factory=list)
    resources: list[FileRef] = Field(default_factory=list)


class GradeBand(AppModel):
    grade: str
    min: float
    max: float
    points: float = 0
    remark: str = ""


class GradeScale(TenantDocument):
    name: str
    bands: list[GradeBand] = Field(default_factory=list)
    pass_percentage: float = 33
    is_default: bool = False

    def grade_for(self, percentage: float) -> GradeBand | None:
        for band in self.bands:
            if band.min <= percentage <= band.max:
                return band
        return None


def parse_clock(value: str) -> time | None:
    try:
        hours, minutes = value.split(":")[:2]
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return None
