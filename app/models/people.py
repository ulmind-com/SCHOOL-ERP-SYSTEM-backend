"""Students, guardians, staff and the admission pipeline."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import EmailStr, Field, computed_field

from app.models.base import (
    Address,
    AppModel,
    ContactInfo,
    FileRef,
    PyObjectId,
    TenantDocument,
)


class Gender(StrEnum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNDISCLOSED = "undisclosed"


class StudentStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    GRADUATED = "graduated"
    TRANSFERRED = "transferred"
    DROPPED = "dropped"
    SUSPENDED = "suspended"


class StaffStatus(StrEnum):
    ACTIVE = "active"
    ON_LEAVE = "on_leave"
    RESIGNED = "resigned"
    RETIRED = "retired"
    TERMINATED = "terminated"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    VISITING = "visiting"
    INTERN = "intern"


class PersonName(AppModel):
    first_name: str
    middle_name: str = ""
    last_name: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.middle_name, self.last_name) if p)


class MedicalInfo(AppModel):
    blood_group: str = ""
    allergies: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    medications: str = ""
    emergency_contact_name: str = ""
    emergency_contact_phone: str = ""
    doctor_name: str = ""
    doctor_phone: str = ""
    notes: str = ""


class PreviousSchool(AppModel):
    name: str = ""
    board: str = ""
    last_class: str = ""
    year_of_leaving: str = ""
    percentage: float | None = None
    tc_number: str = ""


class Student(TenantDocument):
    # Identity
    admission_number: str
    roll_number: str = ""
    registration_number: str = ""
    first_name: str
    middle_name: str = ""
    last_name: str = ""
    date_of_birth: datetime | None = None
    gender: Gender = Gender.UNDISCLOSED
    photo: FileRef | None = None

    # Placement
    current_class_id: PyObjectId | None = None
    current_section_id: PyObjectId | None = None
    academic_year_id: PyObjectId | None = None
    program_id: PyObjectId | None = None
    department_id: PyObjectId | None = None
    semester: int | None = None
    stream: str = ""
    house: str = ""

    # Admission
    admission_date: datetime | None = None
    admission_class_id: PyObjectId | None = None
    status: StudentStatus = StudentStatus.ACTIVE
    status_changed_on: datetime | None = None
    status_reason: str = ""

    # Contact
    contact: ContactInfo = Field(default_factory=ContactInfo)
    address: Address = Field(default_factory=Address)
    permanent_address: Address | None = None

    # Background
    nationality: str = "Indian"
    religion: str = ""
    category: str = ""              # General / OBC / SC / ST …
    mother_tongue: str = ""
    aadhaar_number: str = ""
    is_differently_abled: bool = False

    # Related records
    guardian_ids: list[PyObjectId] = Field(default_factory=list)
    primary_guardian_id: PyObjectId | None = None
    user_id: PyObjectId | None = None
    sibling_ids: list[PyObjectId] = Field(default_factory=list)

    medical: MedicalInfo = Field(default_factory=MedicalInfo)
    previous_school: PreviousSchool | None = None
    documents: list[FileRef] = Field(default_factory=list)

    # Facilities
    uses_transport: bool = False
    transport_route_id: PyObjectId | None = None
    is_hosteller: bool = False
    hostel_room_id: PyObjectId | None = None

    # Finance
    fee_structure_id: PyObjectId | None = None
    fee_concession_percent: float = 0
    outstanding_amount: float = 0

    notes: str = ""
    tags: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.middle_name, self.last_name) if p)


class Enrollment(TenantDocument):
    """One student's placement for one academic year. Keeps history when a
    student moves up a class, so last year's marks still resolve correctly."""

    student_id: PyObjectId
    academic_year_id: PyObjectId
    class_id: PyObjectId
    section_id: PyObjectId | None = None
    roll_number: str = ""
    semester: int | None = None
    enrolled_on: datetime | None = None
    status: str = "active"          # active | promoted | detained | left
    result: str = ""                # pass | fail | pending
    remarks: str = ""


class Guardian(TenantDocument):
    full_name: str
    relation: str = "father"        # father | mother | guardian | other
    student_ids: list[PyObjectId] = Field(default_factory=list)
    contact: ContactInfo = Field(default_factory=ContactInfo)
    address: Address | None = None
    occupation: str = ""
    designation: str = ""
    employer: str = ""
    annual_income: float | None = None
    qualification: str = ""
    photo: FileRef | None = None
    aadhaar_number: str = ""
    user_id: PyObjectId | None = None
    is_emergency_contact: bool = False
    can_pick_up: bool = True
    notes: str = ""


class Qualification(AppModel):
    degree: str = ""
    specialization: str = ""
    institution: str = ""
    year: str = ""
    grade: str = ""


class Experience(AppModel):
    organization: str = ""
    designation: str = ""
    from_year: str = ""
    to_year: str = ""
    description: str = ""


class BankDetails(AppModel):
    account_holder: str = ""
    account_number: str = ""
    bank_name: str = ""
    branch: str = ""
    ifsc: str = ""
    pan: str = ""
    uan: str = ""


class Staff(TenantDocument):
    employee_id: str
    first_name: str
    middle_name: str = ""
    last_name: str = ""
    date_of_birth: datetime | None = None
    gender: Gender = Gender.UNDISCLOSED
    photo: FileRef | None = None

    # Role at the institution
    designation: str = ""
    department_id: PyObjectId | None = None
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    is_teaching: bool = True
    subject_ids: list[PyObjectId] = Field(default_factory=list)
    reports_to_id: PyObjectId | None = None

    joining_date: datetime | None = None
    confirmation_date: datetime | None = None
    leaving_date: datetime | None = None
    status: StaffStatus = StaffStatus.ACTIVE

    contact: ContactInfo = Field(default_factory=ContactInfo)
    address: Address = Field(default_factory=Address)

    qualifications: list[Qualification] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    total_experience_years: float = 0
    specializations: list[str] = Field(default_factory=list)

    # Payroll
    bank: BankDetails = Field(default_factory=BankDetails)
    basic_salary: float = 0
    salary_structure_id: PyObjectId | None = None

    user_id: PyObjectId | None = None
    documents: list[FileRef] = Field(default_factory=list)
    emergency_contact_name: str = ""
    emergency_contact_phone: str = ""
    blood_group: str = ""
    marital_status: str = ""
    notes: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.middle_name, self.last_name) if p)


class AdmissionEnquiry(TenantDocument):
    student_name: str
    guardian_name: str = ""
    phone: str
    email: EmailStr | None = None
    class_interested_id: PyObjectId | None = None
    class_interested_name: str = ""
    source: str = "walk_in"         # walk_in | website | referral | phone | campaign
    status: str = "new"             # new | contacted | visited | converted | lost
    assigned_to: PyObjectId | None = None
    follow_up_on: datetime | None = None
    notes: str = ""
    history: list[dict] = Field(default_factory=list)


class AdmissionApplication(TenantDocument):
    application_number: str
    enquiry_id: PyObjectId | None = None
    first_name: str
    middle_name: str = ""
    last_name: str = ""
    date_of_birth: datetime | None = None
    gender: Gender = Gender.UNDISCLOSED
    class_applied_id: PyObjectId | None = None
    academic_year_id: PyObjectId | None = None
    program_id: PyObjectId | None = None

    guardian_name: str = ""
    guardian_relation: str = "father"
    contact: ContactInfo = Field(default_factory=ContactInfo)
    address: Address = Field(default_factory=Address)
    previous_school: PreviousSchool | None = None

    status: str = "submitted"       # submitted | under_review | shortlisted | interview |
                                    # approved | rejected | enrolled | withdrawn
    stage_notes: list[dict] = Field(default_factory=list)
    score: float | None = None
    interview_on: datetime | None = None
    decided_by: PyObjectId | None = None
    decided_on: datetime | None = None
    rejection_reason: str = ""

    application_fee_paid: bool = False
    documents: list[FileRef] = Field(default_factory=list)
    converted_student_id: PyObjectId | None = None


class Alumni(TenantDocument):
    student_id: PyObjectId | None = None
    full_name: str
    batch_year: int | None = None
    program_name: str = ""
    contact: ContactInfo = Field(default_factory=ContactInfo)
    current_organization: str = ""
    current_designation: str = ""
    city: str = ""
    linkedin: str = ""
    photo: FileRef | None = None
    is_mentor: bool = False
    notes: str = ""


def age_on(dob: datetime | str | None, on: date | None = None) -> int | None:
    """Age in whole years, tolerant of a date of birth stored as a string.

    Documents written before the admission form validated its payload hold an
    ISO string here. Rather than let one such row turn a profile page into a
    500, parse what we find and give up quietly if it is not a date at all.
    """
    if not dob:
        return None
    if isinstance(dob, str):
        try:
            dob = datetime.fromisoformat(dob.replace("Z", "+00:00"))
        except ValueError:
            return None
    on = on or date.today()
    birth = dob.date() if isinstance(dob, datetime) else dob
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))
