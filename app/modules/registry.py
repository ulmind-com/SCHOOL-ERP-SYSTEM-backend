"""Every resource that is plain CRUD, declared in one place.

Modules that need more than CRUD (students, attendance, fees, exams…) keep
their own router and mount these alongside it.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.crud import Resource, build_crud_router
from app.db.mongo import C
from app.models import academics as ac
from app.models import communication as comm
from app.models import facilities as fac
from app.models import finance as fin
from app.models import hr
from app.models import operations as ops
from app.models import people as ppl

RESOURCES: list[Resource] = [
    # ── Academics ─────────────────────────────────────────────────────────
    Resource(
        name="academic-years", collection=C.ACADEMIC_YEARS, module="academic_years",
        model=ac.AcademicYear, label="Academic Year", plural="Academic Years",
        tags=["Academics"], search_fields=["name"], filters=["status", "is_current"],
        sortable=["start_date", "name", "created_at"], default_sort_dir="desc",
        unique_fields=["name"],
    ),
    Resource(
        name="terms", collection=C.TERMS, module="academic_years", model=ac.Term,
        tags=["Academics"], search_fields=["name"],
        filters=["academic_year_id", "type", "is_current"], sortable=["order", "start_date"],
        default_sort_dir="asc",
    ),
    Resource(
        name="departments", collection=C.DEPARTMENTS, module="departments",
        model=ac.Department, tags=["Academics"], search_fields=["name", "code"],
        filters=["is_active", "head_staff_id"], sortable=["name", "created_at"],
        default_sort_dir="asc", unique_fields=["code"],
    ),
    Resource(
        name="programs", collection=C.PROGRAMS, module="programs", model=ac.Program,
        tags=["Academics"], search_fields=["name", "code"],
        filters=["department_id", "level", "is_active"], sortable=["name", "created_at"],
        default_sort_dir="asc", unique_fields=["code"],
    ),
    Resource(
        name="classes", collection=C.CLASSES, module="classes", model=ac.SchoolClass,
        label="Class", plural="Classes", tags=["Academics"], search_fields=["name", "stream"],
        filters=["academic_year_id", "program_id", "department_id", "is_active", "semester"],
        sortable=["order", "numeric_level", "name"], default_sort_dir="asc",
    ),
    Resource(
        name="sections", collection=C.SECTIONS, module="classes", model=ac.Section,
        tags=["Academics"], search_fields=["name", "room"],
        filters=["class_id", "academic_year_id", "class_teacher_id", "is_active"],
        sortable=["name", "created_at"], default_sort_dir="asc",
    ),
    Resource(
        name="subjects", collection=C.SUBJECTS, module="subjects", model=ac.Subject,
        tags=["Academics"], search_fields=["name", "code", "short_name"],
        filters=["class_id", "department_id", "program_id", "type", "is_active"],
        sortable=["order", "name"], default_sort_dir="asc", unique_fields=["code"],
    ),
    Resource(
        name="subject-assignments", collection=C.SUBJECT_ASSIGNMENTS, module="subjects",
        model=ac.SubjectAssignment, label="Subject Assignment", tags=["Academics"],
        filters=["subject_id", "section_id", "staff_id", "academic_year_id"],
    ),
    Resource(
        name="periods", collection=C.PERIODS, module="timetable", model=ac.Period,
        tags=["Timetable"], search_fields=["name"], filters=["academic_year_id", "is_break"],
        sortable=["order"], default_sort_dir="asc",
    ),
    Resource(
        name="timetable-slots", collection=C.TIMETABLE_SLOTS, module="timetable",
        model=ac.TimetableSlot, label="Timetable Slot", plural="Timetable Slots",
        tags=["Timetable"], search_fields=["period_name", "room"],
        filters=["section_id", "class_id", "staff_id", "day_of_week", "academic_year_id",
                 "subject_id", "is_published"],
        sortable=["day_of_week", "start_time"], default_sort_dir="asc",
    ),
    Resource(
        name="syllabus", collection=C.SYLLABUS, module="syllabus", model=ac.SyllabusUnit,
        label="Syllabus Unit", plural="Syllabus", tags=["Academics"],
        search_fields=["title"], filters=["subject_id", "class_id", "completed"],
        sortable=["order", "created_at"], default_sort_dir="asc",
    ),
    Resource(
        name="grade-scales", collection=C.GRADE_SCALES, module="exams", model=ac.GradeScale,
        label="Grade Scale", tags=["Exams"], search_fields=["name"], filters=["is_default"],
        unique_fields=["name"],
    ),

    # ── People ────────────────────────────────────────────────────────────
    Resource(
        name="guardians", collection=C.GUARDIANS, module="guardians", model=ppl.Guardian,
        tags=["People"], search_fields=["full_name", "contact.phone", "contact.email"],
        filters=["relation", "is_emergency_contact"], sortable=["full_name", "created_at"],
    ),
    Resource(
        name="alumni", collection=C.ALUMNI, module="alumni", model=ppl.Alumni,
        label="Alumnus", plural="Alumni", tags=["People"],
        search_fields=["full_name", "current_organization", "program_name"],
        filters=["batch_year", "is_mentor"], sortable=["batch_year", "full_name"],
    ),
    Resource(
        name="admission-enquiries", collection=C.ADMISSION_ENQUIRIES, module="admissions",
        model=ppl.AdmissionEnquiry, label="Enquiry", plural="Admission Enquiries",
        tags=["Admissions"], search_fields=["student_name", "guardian_name", "phone"],
        filters=["status", "source", "assigned_to", "class_interested_id"],
        sortable=["follow_up_on", "created_at"],
    ),

    # ── Operations ────────────────────────────────────────────────────────
    Resource(
        name="assignments", collection=C.ASSIGNMENTS, module="assignments",
        model=ops.Assignment, tags=["Assignments"], search_fields=["title", "description"],
        filters=["subject_id", "class_id", "status", "type", "assigned_by"],
        sortable=["due_date", "created_at"],
    ),
    Resource(
        name="materials", collection=C.LMS_MATERIALS, module="lms",
        model=ops.LearningMaterial, label="Learning Material", plural="Learning Materials",
        tags=["Learning"], search_fields=["title", "description", "tags"],
        filters=["subject_id", "class_id", "type", "is_published"],
        sortable=["created_at", "title"],
    ),
    Resource(
        name="certificates", collection=C.CERTIFICATES, module="certificates",
        model=ops.Certificate, tags=["Certificates"],
        search_fields=["serial_number", "title"],
        filters=["type", "status", "student_id", "staff_id"],
        sortable=["issued_on", "created_at"], unique_fields=["serial_number"],
        generated_fields=["serial_number"],
    ),

    # ── Finance ───────────────────────────────────────────────────────────
    Resource(
        name="fee-heads", collection=C.FEE_HEADS, module="fees", model=fin.FeeHead,
        label="Fee Head", tags=["Finance"], search_fields=["name", "code"],
        filters=["category", "is_active", "is_recurring"], sortable=["name"],
        default_sort_dir="asc", unique_fields=["code"],
    ),
    Resource(
        name="fee-structures", collection=C.FEE_STRUCTURES, module="fees",
        model=fin.FeeStructure, label="Fee Structure", tags=["Finance"],
        search_fields=["name"], filters=["academic_year_id", "is_active", "program_id"],
        sortable=["name", "created_at"],
    ),
    Resource(
        name="discounts", collection=C.DISCOUNTS, module="scholarships", model=fin.Discount,
        label="Discount", plural="Discounts & Scholarships", tags=["Finance"],
        search_fields=["name", "code", "reason"],
        filters=["student_id", "status", "type", "academic_year_id"],
        sortable=["created_at"], unique_fields=["code"],
    ),
    Resource(
        name="expenses", collection=C.EXPENSES, module="expenses", model=fin.Expense,
        tags=["Finance"], search_fields=["title", "vendor", "invoice_number"],
        filters=["category", "status", "department_id"], sortable=["spent_on", "created_at"],
    ),
    Resource(
        name="salary-structures", collection=C.SALARY_STRUCTURES, module="payroll",
        model=fin.SalaryStructure, label="Salary Structure", tags=["Payroll"],
        search_fields=["name", "designation"], filters=["staff_id", "is_active"],
    ),

    # ── HR ────────────────────────────────────────────────────────────────
    Resource(
        name="leave-types", collection=C.LEAVE_TYPES, module="leaves", model=hr.LeaveType,
        label="Leave Type", tags=["HR"], search_fields=["name", "code"],
        filters=["is_active", "is_paid", "applies_to"], sortable=["name"],
        default_sort_dir="asc", unique_fields=["code"],
    ),
    Resource(
        name="leave-requests", collection=C.LEAVE_REQUESTS, module="leaves",
        model=hr.LeaveRequest, label="Leave Request", plural="Leave Requests", tags=["HR"],
        search_fields=["staff_name", "reason"],
        filters=["staff_id", "status", "leave_type_id"],
        sortable=["from_date", "created_at"],
    ),
    Resource(
        name="staff-attendance", collection=C.STAFF_ATTENDANCE, module="staff_attendance",
        model=hr.StaffAttendance, label="Staff Attendance Record",
        plural="Staff Attendance", tags=["HR"],
        filters=["staff_id", "status", "date"], sortable=["date"],
    ),
    Resource(
        name="exams", collection=C.EXAMS, module="exams", model=ops.Exam,
        label="Exam", plural="Exams", tags=["Exams"],
        search_fields=["name", "instructions"],
        filters=["academic_year_id", "term_id", "type", "status"],
        sortable=["start_date", "name", "created_at"], default_sort_dir="desc",
    ),
    Resource(
        name="marks", collection=C.MARKS, module="exams", model=ops.Mark,
        label="Mark", plural="Marks", tags=["Exams"],
        filters=["exam_id", "student_id", "subject_id", "section_id", "class_id"],
        sortable=["created_at"],
    ),
    Resource(
        name="exam-schedules", collection=C.EXAM_SCHEDULES, module="exams",
        model=ops.ExamSchedule, label="Exam Schedule", plural="Exam Schedules",
        tags=["Exams"], filters=["exam_id", "subject_id", "class_id"],
        sortable=["date"], default_sort_dir="asc",
    ),
    Resource(
        name="report-cards", collection=C.REPORT_CARDS, module="results",
        model=ops.ReportCard, label="Report Card", plural="Report Cards",
        tags=["Results"], filters=["student_id", "exam_id", "class_id", "section_id", "result"],
        sortable=["created_at"], read_only=True,
    ),
    Resource(
        name="appraisals", collection=C.APPRAISALS, module="appraisals", model=hr.Appraisal,
        tags=["HR"], filters=["staff_id", "period", "status", "reviewer_id"],
        sortable=["created_at"],
    ),

    # ── Facilities ────────────────────────────────────────────────────────
    Resource(
        name="library/items", collection=C.LIBRARY_ITEMS, module="library",
        model=fac.LibraryItem, label="Library Item", plural="Library Items",
        tags=["Library"], search_fields=["title", "author", "isbn", "accession_number"],
        filters=["category", "status", "subject_area", "language"],
        sortable=["title", "created_at"], default_sort_dir="asc",
        unique_fields=["accession_number"],
    ),
    Resource(
        name="transport/vehicles", collection=C.TRANSPORT_VEHICLES, module="transport",
        model=fac.Vehicle, label="Vehicle", plural="Vehicles", tags=["Transport"],
        search_fields=["registration_number", "model", "driver_name"],
        filters=["status", "type"], sortable=["registration_number"],
        default_sort_dir="asc", unique_fields=["registration_number"],
    ),
    Resource(
        name="transport/routes", collection=C.TRANSPORT_ROUTES, module="transport",
        model=fac.TransportRoute, label="Route", plural="Routes", tags=["Transport"],
        search_fields=["name", "code", "start_point", "end_point"],
        filters=["is_active", "vehicle_id"], sortable=["name"], default_sort_dir="asc",
        unique_fields=["code"],
    ),
    Resource(
        name="transport/stops", collection=C.TRANSPORT_STOPS, module="transport",
        model=fac.TransportStop, label="Stop", plural="Stops", tags=["Transport"],
        search_fields=["name", "landmark"], filters=["route_id"], sortable=["order"],
        default_sort_dir="asc",
    ),
    Resource(
        name="transport/allocations", collection=C.TRANSPORT_ALLOCATIONS, module="transport",
        model=fac.TransportAllocation, label="Allocation", plural="Transport Allocations",
        tags=["Transport"], filters=["student_id", "route_id", "status", "academic_year_id"],
    ),
    Resource(
        name="hostel/blocks", collection=C.HOSTELS, module="hostel", model=fac.Hostel,
        label="Hostel", plural="Hostels", tags=["Hostel"], search_fields=["name"],
        filters=["type", "is_active", "warden_staff_id"], sortable=["name"],
        default_sort_dir="asc", unique_fields=["name"],
    ),
    Resource(
        name="hostel/rooms", collection=C.HOSTEL_ROOMS, module="hostel", model=fac.HostelRoom,
        label="Room", plural="Hostel Rooms", tags=["Hostel"], search_fields=["room_number"],
        filters=["hostel_id", "status", "type", "floor"], sortable=["room_number"],
        default_sort_dir="asc",
    ),
    Resource(
        name="hostel/allocations", collection=C.HOSTEL_ALLOCATIONS, module="hostel",
        model=fac.HostelAllocation, label="Allocation", plural="Hostel Allocations",
        tags=["Hostel"], filters=["student_id", "hostel_id", "room_id", "status"],
    ),
    Resource(
        name="inventory/items", collection=C.INVENTORY_ITEMS, module="inventory",
        model=fac.InventoryItem, label="Item", plural="Inventory Items", tags=["Inventory"],
        search_fields=["name", "sku", "supplier"],
        filters=["category", "condition", "is_asset", "location"], sortable=["name"],
        default_sort_dir="asc", unique_fields=["sku"],
    ),
    Resource(
        name="inventory/transactions", collection=C.INVENTORY_TXNS, module="inventory",
        model=fac.InventoryTransaction, label="Transaction", plural="Inventory Transactions",
        tags=["Inventory"], filters=["item_id", "type", "issued_to_id"],
        sortable=["occurred_on", "created_at"],
    ),
    Resource(
        name="visitors", collection=C.VISITORS, module="visitors", model=fac.Visitor,
        tags=["Visitors"], search_fields=["full_name", "phone", "pass_number"],
        filters=["status", "visiting_type", "host_staff_id"],
        sortable=["checked_in_at", "created_at"],
    ),

    # ── Communication ─────────────────────────────────────────────────────
    Resource(
        name="announcements", collection=C.ANNOUNCEMENTS, module="announcements",
        model=comm.Announcement, tags=["Communication"], search_fields=["title", "body"],
        filters=["category", "priority", "status", "pin_to_top"],
        sortable=["published_at", "created_at"],
    ),
    Resource(
        name="events", collection=C.EVENTS, module="events", model=comm.Event,
        tags=["Communication"], search_fields=["title", "description", "location"],
        filters=["category", "status", "is_holiday"], sortable=["start_at", "created_at"],
    ),
    Resource(
        name="complaints", collection=C.COMPLAINTS, module="complaints", model=comm.Complaint,
        plural="Complaints & Helpdesk", tags=["Helpdesk"],
        search_fields=["title", "description", "ticket_number"],
        filters=["status", "category", "priority", "assigned_to", "raised_by"],
        sortable=["created_at"],
    ),
]


def build_registry_router() -> APIRouter:
    router = APIRouter()
    for resource in RESOURCES:
        router.include_router(build_crud_router(resource))
    return router


RESOURCES_BY_NAME = {r.name: r for r in RESOURCES}
