"""Index definitions, applied on startup.

Every tenant-scoped uniqueness constraint is compound on ``tenant_id`` — two
schools may each have a student with roll number 1, and neither may have two.
"""

from __future__ import annotations

import logging

from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel

from app.core.config import settings
from app.db.mongo import C, get_database

log = logging.getLogger("scholarly.db.indexes")

T = "tenant_id"


def _u(*fields: str) -> IndexModel:
    """Unique index scoped to one institution.

    Partial on ``is_deleted: false`` so a soft-deleted record stops occupying
    its admission number / code and the value can be reused.
    (Mongo's partial filters accept ``$eq`` but not ``$ne``.)
    """
    keys = [(T, ASCENDING)] + [(f, ASCENDING) for f in fields]
    return IndexModel(
        keys, unique=True, partialFilterExpression={"is_deleted": {"$eq": False}}
    )


def _i(*fields: str) -> IndexModel:
    return IndexModel([(T, ASCENDING)] + [(f, ASCENDING) for f in fields])


INDEXES: dict[str, list[IndexModel]] = {
    # ── Platform (not tenant-scoped) ──────────────────────────────────────
    C.TENANTS: [
        IndexModel([("slug", ASCENDING)], unique=True),
        IndexModel([("status", ASCENDING), ("deployment", ASCENDING)]),
        IndexModel([("subscription_valid_till", ASCENDING)]),
        IndexModel([("name", TEXT), ("slug", TEXT), ("contact.email", TEXT)]),
    ],
    C.PLANS: [IndexModel([("key", ASCENDING)], unique=True),
              IndexModel([("sort_order", ASCENDING)])],
    C.PLATFORM_USERS: [IndexModel([("email", ASCENDING)], unique=True)],
    C.SUBSCRIPTIONS: [IndexModel([("tenant_id", ASCENDING), ("status", ASCENDING)]),
                      IndexModel([("current_period_end", ASCENDING)])],
    C.PLATFORM_INVOICES: [IndexModel([("number", ASCENDING)], unique=True),
                          IndexModel([("tenant_id", ASCENDING), ("status", ASCENDING)])],
    C.PLATFORM_AUDIT: [IndexModel([("created_at", DESCENDING)]),
                       IndexModel([("actor_id", ASCENDING)])],

    # ── Identity ──────────────────────────────────────────────────────────
    C.USERS: [
        _u("email"),
        # Not unique: a parent and their child can legitimately share one number.
        _i("phone_digits"),
        _i("status"), _i("student_id"), _i("staff_id"), _i("guardian_id"),
        IndexModel([(T, ASCENDING), ("full_name", TEXT), ("email", TEXT), ("phone", TEXT)]),
    ],
    C.ROLES: [_u("key"), _i("portal")],
    C.SESSIONS: [
        IndexModel([("token_hash", ASCENDING)], unique=True),
        IndexModel([("user_id", ASCENDING)]),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0),
    ],
    C.AUDIT: [_i("created_at"), _i("actor_id"), _i("entity_type", "entity_id")],
    C.NOTIFICATIONS: [_i("user_id", "created_at"), _i("read_at")],
    C.COUNTERS: [IndexModel([(T, ASCENDING), ("key", ASCENDING)], unique=True)],

    # ── Academics ─────────────────────────────────────────────────────────
    C.ACADEMIC_YEARS: [_u("name"), _i("is_current")],
    C.TERMS: [_i("academic_year_id")],
    C.DEPARTMENTS: [_u("code"), _i("name")],
    C.PROGRAMS: [_u("code"), _i("department_id")],
    C.CLASSES: [_u("name", "academic_year_id"), _i("program_id"), _i("academic_year_id")],
    C.SECTIONS: [_u("class_id", "name"), _i("class_teacher_id")],
    C.SUBJECTS: [_u("code"), _i("class_id"), _i("department_id")],
    C.SUBJECT_ASSIGNMENTS: [_i("subject_id", "section_id"), _i("staff_id")],
    C.TIMETABLE_SLOTS: [_i("section_id", "day_of_week"), _i("staff_id", "day_of_week"),
                        _i("academic_year_id")],
    C.PERIODS: [_i("academic_year_id", "order")],
    C.SYLLABUS: [_i("subject_id", "order")],

    # ── People ────────────────────────────────────────────────────────────
    C.STUDENTS: [
        _u("admission_number"),
        _i("status"), _i("current_class_id", "current_section_id"), _i("user_id"),
        IndexModel([(T, ASCENDING), ("roll_number", ASCENDING),
                    ("current_section_id", ASCENDING)]),
        IndexModel([(T, ASCENDING), ("first_name", TEXT), ("last_name", TEXT),
                    ("admission_number", TEXT), ("contact.phone", TEXT),
                    ("contact.email", TEXT)]),
    ],
    C.ENROLLMENTS: [_u("student_id", "academic_year_id"), _i("section_id"), _i("status")],
    C.GUARDIANS: [_i("student_ids"), _i("contact.phone"), _i("user_id"),
                  IndexModel([(T, ASCENDING), ("full_name", TEXT), ("contact.phone", TEXT)])],
    C.STAFF: [
        _u("employee_id"), _i("department_id"), _i("status"), _i("user_id"),
        IndexModel([(T, ASCENDING), ("first_name", TEXT), ("last_name", TEXT),
                    ("employee_id", TEXT), ("contact.phone", TEXT)]),
    ],
    C.ADMISSION_ENQUIRIES: [_i("status", "created_at"), _i("assigned_to")],
    C.ADMISSION_APPLICATIONS: [_u("application_number"), _i("status"), _i("class_applied_id")],
    C.ALUMNI: [_i("batch_year"), _i("student_id")],

    # ── Operations ────────────────────────────────────────────────────────
    C.ATTENDANCE: [
        _u("student_id", "date", "session_key"),
        _i("section_id", "date"), _i("date", "status"), _i("student_id", "date"),
    ],
    C.ATTENDANCE_SESSIONS: [_u("section_id", "date", "session_key"), _i("taken_by")],
    C.ASSIGNMENTS: [_i("section_id", "due_date"), _i("subject_id"), _i("created_by")],
    C.SUBMISSIONS: [_u("assignment_id", "student_id"), _i("status")],
    C.EXAMS: [_i("academic_year_id", "start_date"), _i("status")],
    C.EXAM_SCHEDULES: [_i("exam_id", "date"), _i("subject_id")],
    C.MARKS: [_u("exam_id", "student_id", "subject_id"), _i("student_id"), _i("exam_id")],
    C.GRADE_SCALES: [_u("name")],
    C.REPORT_CARDS: [_u("student_id", "exam_id"), _i("published_at")],
    C.LMS_MATERIALS: [_i("subject_id"), _i("section_id"), _i("created_at")],
    C.CERTIFICATES: [_u("serial_number"), _i("student_id", "type")],

    # ── Finance ───────────────────────────────────────────────────────────
    C.FEE_HEADS: [_u("code")],
    C.FEE_STRUCTURES: [_u("name", "academic_year_id"), _i("class_ids")],
    C.FEE_INVOICES: [
        _u("number"), _i("student_id", "status"), _i("status", "due_date"),
        _i("academic_year_id"),
    ],
    C.PAYMENTS: [_u("receipt_number"), _i("student_id", "paid_at"),
                 _i("invoice_id"), _i("paid_at"), _i("method")],
    C.DISCOUNTS: [_u("code"), _i("student_id")],
    C.EXPENSES: [_i("category", "spent_on"), _i("status")],
    C.PAYROLL_RUNS: [_u("period"), _i("status")],
    C.PAYSLIPS: [_u("payroll_run_id", "staff_id"), _i("staff_id")],
    C.SALARY_STRUCTURES: [_i("staff_id")],

    # ── HR ────────────────────────────────────────────────────────────────
    C.LEAVE_TYPES: [_u("code")],
    C.LEAVE_REQUESTS: [_i("staff_id", "status"), _i("status", "from_date")],
    C.STAFF_ATTENDANCE: [_u("staff_id", "date"), _i("date", "status")],
    C.APPRAISALS: [_i("staff_id", "period")],

    # ── Facilities ────────────────────────────────────────────────────────
    C.LIBRARY_ITEMS: [_u("accession_number"), _i("category"),
                      IndexModel([(T, ASCENDING), ("title", TEXT), ("author", TEXT),
                                  ("isbn", TEXT)])],
    C.LIBRARY_LOANS: [_i("item_id", "status"), _i("borrower_id", "status"), _i("due_date")],
    C.TRANSPORT_ROUTES: [_u("code"), _i("vehicle_id")],
    C.TRANSPORT_VEHICLES: [_u("registration_number")],
    C.TRANSPORT_STOPS: [_i("route_id", "order")],
    C.TRANSPORT_ALLOCATIONS: [_u("student_id", "academic_year_id"), _i("route_id")],
    C.HOSTELS: [_u("name")],
    C.HOSTEL_ROOMS: [_u("hostel_id", "room_number")],
    C.HOSTEL_ALLOCATIONS: [_i("room_id", "status"), _u("student_id", "academic_year_id")],
    C.INVENTORY_ITEMS: [_u("sku"), _i("category")],
    C.INVENTORY_TXNS: [_i("item_id", "created_at")],
    C.VISITORS: [_i("checked_in_at"), _i("host_staff_id")],

    # ── Communication ─────────────────────────────────────────────────────
    C.ANNOUNCEMENTS: [_i("published_at"), _i("audience.roles"), _i("status")],
    C.EVENTS: [_i("start_at"), _i("category")],
    C.THREADS: [_i("participant_ids", "last_message_at")],
    C.MESSAGES: [_i("thread_id", "created_at")],
    C.COMPLAINTS: [_i("status", "created_at"), _i("raised_by")],

    # ── Delivery & devices ────────────────────────────────────────────────
    C.NOTIFICATION_DELIVERIES: [_i("created_at"), _i("channel", "status")],
    C.MESSAGE_TEMPLATES: [_u("key")],
    C.BIOMETRIC_DEVICES: [_u("serial_number"), _i("status")],
    C.BIOMETRIC_PUNCHES: [
        _i("device_serial", "punched_at"), _i("biometric_id", "punched_at"),
        _i("processed"),
    ],
    C.VEHICLE_POSITIONS: [
        _i("vehicle_id", "recorded_at"), _i("trip_id"),
        IndexModel([("recorded_at", ASCENDING)], expireAfterSeconds=60 * 60 * 24 * 30),
    ],
    C.VEHICLE_TRIPS: [_i("vehicle_id", "started_at"), _i("route_id", "status")],
    C.PAYMENT_ORDERS: [_u("order_reference"), _i("student_id", "status"),
                       _i("invoice_id"), _i("status", "created_at")],
    C.ONLINE_CLASSES: [_i("section_id", "starts_at"), _i("staff_id", "starts_at"),
                       _i("status")],
    C.AI_CONVERSATIONS: [_i("user_id", "created_at")],

    # ── Shared ────────────────────────────────────────────────────────────
    C.DOCUMENTS: [_i("owner_type", "owner_id"), _i("category"), _i("created_at")],
}


async def ensure_indexes() -> dict[str, int]:
    """Idempotent. Safe to run on every boot."""
    db = get_database()
    created: dict[str, int] = {}
    for name, models in INDEXES.items():
        if not models:
            continue
        try:
            result = await db[name].create_indexes(models)
            created[name] = len(result)
        except Exception as exc:  # pragma: no cover - index conflicts on upgrade
            log.warning("Index creation skipped for %s: %s", name, exc)
    log.info("Indexes ensured on %d collections (mode=%s)",
             len(created), settings.deployment_mode)
    return created
