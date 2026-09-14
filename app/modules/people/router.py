"""Students and staff — CRUD from the factory plus the operations that aren't."""

from typing import Annotated, Any

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, Request

from app.core.context import AuthContext, TenantContext
from app.core.crud import Resource, build_crud_router, check_unique, make_create_schema
from app.core.deps import TenantDep, require
from app.core.exceptions import ValidationError
from app.core.scoping import (
    assert_may_see_student,
    own_student_scope,
    student_row_scope,
)
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models import people as ppl
from app.models.base import AppModel, Msg
from app.modules.people import service
from app.utils.audit import record

router = APIRouter()


# ── Row-level scoping ─────────────────────────────────────────────────────
#: A parent sees their own children; a student sees only themselves. Applied on
#: top of the tenant filter, so a portal user cannot widen it by guessing query
#: parameters. Shared with every other student-keyed resource — see
#: ``app.core.scoping``.
student_scope = own_student_scope


async def staff_scope(auth: AuthContext, tenant: TenantContext) -> dict[str, Any]:
    if auth.staff_id and not auth.can("staff:update"):
        return {"_id": auth.staff_id}
    return {}


# ── Hooks ─────────────────────────────────────────────────────────────────
async def before_student_create(
    data: dict, auth: AuthContext, tenant: TenantContext, repo: Repository
) -> dict:
    if not data.get("admission_number"):
        data["admission_number"] = await service.generate_admission_number(tenant)
    section_id = data.get("current_section_id")
    await service.check_section_capacity(tenant.id, section_id)
    if not data.get("roll_number"):
        data["roll_number"] = await service.next_roll_number(tenant.id, section_id)
    if not data.get("admission_date"):
        from app.models.base import utcnow

        data["admission_date"] = utcnow()
    if not data.get("academic_year_id") and tenant.current_academic_year_id:
        data["academic_year_id"] = tenant.current_academic_year_id
    return data


async def after_student_create(
    doc: dict, auth: AuthContext, tenant: TenantContext, repo: Repository
) -> None:
    await service.recount_section(tenant.id, doc.get("current_section_id"))
    if doc.get("current_class_id"):
        await Repository(C.ENROLLMENTS, tenant.id, actor_id=auth.user_id).create({
            "student_id": doc["_id"],
            "academic_year_id": doc.get("academic_year_id"),
            "class_id": doc.get("current_class_id"),
            "section_id": doc.get("current_section_id"),
            "roll_number": doc.get("roll_number", ""),
            "enrolled_on": doc.get("admission_date"),
            "status": "active",
        })


async def after_student_update(
    after: dict, before: dict, auth: AuthContext, tenant: TenantContext, repo: Repository
) -> None:
    if before.get("current_section_id") != after.get("current_section_id"):
        await service.recount_section(tenant.id, before.get("current_section_id"))
        await service.recount_section(tenant.id, after.get("current_section_id"))


async def before_staff_create(
    data: dict, auth: AuthContext, tenant: TenantContext, repo: Repository
) -> dict:
    if not data.get("employee_id"):
        data["employee_id"] = await service.generate_employee_id(tenant)
    return data


def with_name(doc: dict) -> dict:
    doc["full_name"] = " ".join(
        p for p in (doc.get("first_name"), doc.get("middle_name"), doc.get("last_name")) if p
    )
    return doc


# ── Resources ─────────────────────────────────────────────────────────────
STUDENTS = Resource(
    name="students", collection=C.STUDENTS, module="students", model=ppl.Student,
    tags=["Students"],
    search_fields=["first_name", "middle_name", "last_name", "admission_number",
                   "roll_number", "contact.phone", "contact.email"],
    filters=["current_class_id", "current_section_id", "status", "gender", "academic_year_id",
             "program_id", "department_id", "house", "category", "is_hosteller",
             "uses_transport", "stream", "semester"],
    sortable=["roll_number", "first_name", "admission_number", "created_at"],
    default_sort_dir="asc",
    unique_fields=["admission_number"],
    limit_key="max_students",
    generated_fields=["admission_number", "roll_number"],
    before_create=before_student_create,
    after_create=after_student_create,
    after_update=after_student_update,
    scope_hook=student_scope,
    serializer=with_name,
)

STAFF = Resource(
    name="staff", collection=C.STAFF, module="staff", model=ppl.Staff,
    label="Staff Member", plural="Staff", tags=["Staff"],
    search_fields=["first_name", "last_name", "employee_id", "designation",
                   "contact.phone", "contact.email"],
    filters=["department_id", "status", "employment_type", "is_teaching", "gender"],
    sortable=["first_name", "employee_id", "joining_date", "created_at"],
    default_sort_dir="asc",
    unique_fields=["employee_id"],
    limit_key="max_staff",
    generated_fields=["employee_id"],
    before_create=before_staff_create,
    scope_hook=staff_scope,
    serializer=with_name,
)

ENROLLMENTS = Resource(
    name="enrollments", collection=C.ENROLLMENTS, module="students", model=ppl.Enrollment,
    tags=["Students"], filters=["student_id", "academic_year_id", "class_id", "section_id",
                                "status"],
    sortable=["created_at"],
    scope_hook=student_row_scope,
)

APPLICATIONS = Resource(
    name="admission-applications", collection=C.ADMISSION_APPLICATIONS, module="admissions",
    model=ppl.AdmissionApplication, label="Application", plural="Admission Applications",
    tags=["Admissions"],
    search_fields=["first_name", "last_name", "application_number", "contact.phone"],
    filters=["status", "class_applied_id", "academic_year_id", "program_id"],
    sortable=["created_at"], unique_fields=["application_number"],
    serializer=with_name,
)

for resource in (STUDENTS, STAFF, ENROLLMENTS, APPLICATIONS):
    router.include_router(build_crud_router(resource))


# ── Bespoke: students ─────────────────────────────────────────────────────
students_extra = APIRouter(prefix="/students", tags=["Students"])

Reader = Annotated[AuthContext, Depends(require("students:read"))]
Writer = Annotated[AuthContext, Depends(require("students:update"))]
Creator = Annotated[AuthContext, Depends(require("students:create"))]


class PromoteRequest(AppModel):
    student_ids: list[str]
    to_class_id: str
    to_section_id: str | None = None
    to_academic_year_id: str
    result: str = "pass"
    reset_roll_numbers: bool = True


class StatusChangeRequest(AppModel):
    status: str
    reason: str = ""


#: The admission form posts a student and a guardian in one body. Both halves go
#: through the same schemas the plain CRUD endpoints use — typing them as bare
#: dicts once meant a date of birth reached Mongo as the string the browser sent
#: and a class id as a string that no ObjectId query could ever match.
StudentAdmitPayload = make_create_schema(
    ppl.Student, "StudentAdmitPayload", optional=set(STUDENTS.generated_fields)
)
GuardianAdmitPayload = make_create_schema(ppl.Guardian, "GuardianAdmitPayload")


class StudentWithGuardianRequest(AppModel):
    """Admitting a student and capturing the parent in one submission — the way
    a front desk actually works."""

    student: StudentAdmitPayload  # type: ignore[valid-type]
    guardians: list[GuardianAdmitPayload] = []  # type: ignore[valid-type]
    create_login: bool = False
    create_guardian_login: bool = False


@students_extra.get("/{student_id}/profile", summary="Full student profile in one call")
async def profile(student_id: str, auth: Reader, tenant: TenantDep):
    await assert_may_see_student(auth, tenant, student_id)
    return await service.student_profile(tenant, student_id)


@students_extra.post("/with-guardians", status_code=201,
                     summary="Admit a student and their guardians together")
async def create_with_guardians(
    payload: StudentWithGuardianRequest, auth: Creator, tenant: TenantDep, request: Request
):
    from app.core.crud import enforce_limit
    from app.modules.users.service import create_user_for_person

    students = Repository(C.STUDENTS, tenant.id, actor_id=auth.user_id)
    guardians = Repository(C.GUARDIANS, tenant.id, actor_id=auth.user_id)
    await enforce_limit(STUDENTS, tenant, students)

    data = await before_student_create(
        payload.student.model_dump(exclude_none=True), auth, tenant, students
    )
    data.pop("guardian_ids", None)
    await check_unique(STUDENTS, students, data)
    student = await students.create(data)

    guardian_ids: list[ObjectId] = []
    logins: list[dict[str, Any]] = []
    skipped: list[str] = []

    for raw in payload.guardians:
        guardian = await guardians.create(
            {**raw.model_dump(exclude_none=True), "student_ids": [student["_id"]]}
        )
        guardian_ids.append(guardian["_id"])
        guardian_email = (guardian.get("contact") or {}).get("email", "")
        if payload.create_guardian_login:
            if guardian_email:
                created = await create_user_for_person(
                    tenant, auth, email=guardian_email,
                    full_name=guardian.get("full_name", ""), role_key="parent",
                    phone=(guardian.get("contact") or {}).get("phone", ""),
                    guardian_id=guardian["_id"],
                )
                if created:
                    logins.append({"for": "parent", **created})
            else:
                skipped.append(
                    f"{guardian.get('full_name') or 'The guardian'} has no email address, "
                    "so no parent login was created"
                )

    if guardian_ids:
        await students.update(student["_id"], {
            "guardian_ids": guardian_ids, "primary_guardian_id": guardian_ids[0]
        })
    await after_student_create(student, auth, tenant, students)

    student_email = (student.get("contact") or {}).get("email", "")
    login = None
    if payload.create_login:
        if student_email:
            login = await create_user_for_person(
                tenant, auth, email=student_email,
                full_name=" ".join(filter(None, [student.get("first_name"),
                                                 student.get("last_name")])),
                role_key="student", phone=(student.get("contact") or {}).get("phone", ""),
                student_id=student["_id"],
            )
            if login:
                logins.append({"for": "student", **login})
        else:
            skipped.append(
                "The student has no email address, so no student login was created"
            )

    await record(auth, "students.admit", entity_type="students", entity_id=student["_id"],
                 entity_label=data.get("admission_number", ""), request=request)
    # Say plainly what happened to the logins. Silence here is how a school ends
    # up believing a parent was emailed when nothing was ever created.
    from app.core.config import settings

    mailed = [entry["email"] for entry in logins]
    if mailed and settings.email_enabled:
        detail = "Student admitted. Sign-in details emailed to " + ", ".join(mailed) + "."
    elif mailed:
        detail = (
            "Student admitted, but no email provider is configured — share the "
            "temporary passwords below yourself."
        )
    else:
        detail = "Student admitted"

    return {
        "id": str(student["_id"]),
        "admission_number": student.get("admission_number"),
        "guardian_ids": [str(g) for g in guardian_ids],
        "login": login,
        "logins": logins,
        "skipped": skipped,
        "email_configured": settings.email_enabled,
        "detail": detail,
    }


@students_extra.post("/promote", summary="Promote a cohort into the next academic year")
async def promote(payload: PromoteRequest, auth: Writer, tenant: TenantDep, request: Request):
    if not payload.student_ids:
        raise ValidationError("Select at least one student")
    result = await service.promote_students(
        tenant, auth,
        student_ids=payload.student_ids,
        to_class_id=payload.to_class_id,
        to_section_id=payload.to_section_id,
        to_academic_year_id=payload.to_academic_year_id,
        result=payload.result,
        reset_roll_numbers=payload.reset_roll_numbers,
    )
    await record(auth, "students.promote", entity_type="students",
                 changes={"count": result["promoted"]}, request=request)
    return result


@students_extra.post("/{student_id}/status", summary="Change a student's status")
async def set_status(
    student_id: str, payload: StatusChangeRequest, auth: Writer, tenant: TenantDep,
    request: Request,
):
    result = await service.change_status(tenant, auth, student_id, payload.status, payload.reason)
    await record(auth, "students.status_change", entity_type="students", entity_id=student_id,
                 changes={"status": payload.status, "reason": payload.reason}, request=request)
    return result


@students_extra.get("/{student_id}/attendance-summary", summary="Attendance at a glance")
async def attendance_summary(student_id: str, auth: Reader, tenant: TenantDep):
    return await service.attendance_summary(tenant.id, ObjectId(student_id))


@students_extra.get("/{student_id}/fee-summary", summary="Billed, paid and outstanding")
async def fee_summary(student_id: str, auth: Reader, tenant: TenantDep):
    return await service.fee_summary(tenant.id, ObjectId(student_id))


@students_extra.post("/{student_id}/guardians", response_model=Msg,
                     summary="Attach an existing guardian")
async def link_guardian(
    student_id: str, auth: Writer, tenant: TenantDep,
    guardian_id: Annotated[str, Body(embed=True)],
):
    sid, gid = ObjectId(student_id), ObjectId(guardian_id)
    await collection(C.STUDENTS).update_one(
        {"_id": sid, "tenant_id": tenant.id}, {"$addToSet": {"guardian_ids": gid}}
    )
    await collection(C.GUARDIANS).update_one(
        {"_id": gid, "tenant_id": tenant.id}, {"$addToSet": {"student_ids": sid}}
    )
    return Msg(detail="Guardian linked")


class RollNumberRequest(AppModel):
    section_id: str
    #: first_name | last_name | admission_number | date_of_birth
    order_by: str = "first_name"
    start_at: int = 1
    #: Off by default, so a mid-year admission slots in without renumbering a
    #: register the class has already written in.
    overwrite: bool = False


@students_extra.post("/assign-roll-numbers", summary="Number a section's students")
async def assign_rolls(
    payload: RollNumberRequest, auth: Writer, tenant: TenantDep, request: Request
):
    result = await service.assign_roll_numbers(
        tenant, auth,
        section_id=payload.section_id, order_by=payload.order_by,
        start_at=payload.start_at, overwrite=payload.overwrite,
    )
    await record(auth, "students.roll_numbers_assigned", entity_type="students",
                 entity_id=payload.section_id,
                 changes={"assigned": result["assigned"], "order_by": payload.order_by},
                 request=request)
    return result


@students_extra.get("/roster/{section_id}", summary="Class roster for a section")
async def roster(section_id: str, auth: Reader, tenant: TenantDep):
    docs = await collection(C.STUDENTS).find(
        {"tenant_id": tenant.id, "current_section_id": ObjectId(section_id),
         "status": "active", "is_deleted": {"$ne": True}},
        {"first_name": 1, "middle_name": 1, "last_name": 1, "roll_number": 1,
         "admission_number": 1, "photo": 1, "gender": 1},
    ).sort([("roll_number", 1)]).to_list(length=500)
    from app.models.base import serialize_doc

    return [with_name(serialize_doc(d) or {}) for d in docs]


router.include_router(students_extra)
