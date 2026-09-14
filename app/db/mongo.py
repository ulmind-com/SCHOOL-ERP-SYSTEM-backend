"""MongoDB connection lifecycle.

Both deployment modes speak to a single database handle. The difference is what
lives inside it: in ``saas`` many tenants share the collections (scoped by
``tenant_id``), in ``dedicated`` there is exactly one tenant. Keeping the shape
identical means a SaaS institution can be lifted into its own deployment later
by copying its documents — no schema rewrite.
"""

from __future__ import annotations

import logging

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)
from pymongo import ReadPreference
from pymongo.errors import ConnectionFailure

from app.core.config import settings

log = logging.getLogger("scholarly.db")


class Database:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None


_state = Database()


async def connect_to_mongo() -> AsyncIOMotorDatabase:
    if _state.db is not None:
        return _state.db
    log.info("Connecting to MongoDB (db=%s)", settings.mongodb_db_name)
    _state.client = AsyncIOMotorClient(
        settings.mongodb_uri,
        maxPoolSize=100,
        minPoolSize=5,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        retryWrites=True,
        tz_aware=True,
        appname=f"{settings.app_name}-{settings.deployment_mode}",
    )
    _state.db = _state.client[settings.mongodb_db_name]
    try:
        await _state.client.admin.command("ping")
        log.info("MongoDB connection established")
    except ConnectionFailure as exc:  # pragma: no cover - network dependent
        log.error("MongoDB connection failed: %s", exc)
        raise
    return _state.db


async def close_mongo_connection() -> None:
    if _state.client is not None:
        _state.client.close()
        _state.client = None
        _state.db = None
        log.info("MongoDB connection closed")


def get_database() -> AsyncIOMotorDatabase:
    if _state.db is None:
        raise RuntimeError("Database not initialised — call connect_to_mongo() first")
    return _state.db


def collection(name: str, *, secondary_ok: bool = False) -> AsyncIOMotorCollection:
    db = get_database()
    if secondary_ok:
        return db.get_collection(name, read_preference=ReadPreference.SECONDARY_PREFERRED)
    return db[name]


async def ping() -> bool:
    try:
        if _state.client is None:
            return False
        await _state.client.admin.command("ping")
        return True
    except Exception:  # pragma: no cover
        return False


# ── Collection names (single source of truth) ─────────────────────────────
class C:
    # Platform (SaaS only — never tenant-scoped)
    TENANTS = "tenants"
    PLANS = "plans"
    SUBSCRIPTIONS = "subscriptions"
    PLATFORM_USERS = "platform_users"
    PLATFORM_INVOICES = "platform_invoices"
    PLATFORM_AUDIT = "platform_audit_logs"

    # Identity (tenant-scoped)
    USERS = "users"
    ROLES = "roles"
    SESSIONS = "sessions"
    AUDIT = "audit_logs"
    NOTIFICATIONS = "notifications"

    # Academics
    ACADEMIC_YEARS = "academic_years"
    TERMS = "terms"
    DEPARTMENTS = "departments"
    PROGRAMS = "programs"
    CLASSES = "classes"
    SECTIONS = "sections"
    SUBJECTS = "subjects"
    SUBJECT_ASSIGNMENTS = "subject_assignments"
    TIMETABLE_SLOTS = "timetable_slots"
    PERIODS = "periods"
    SYLLABUS = "syllabus_units"

    # People
    STUDENTS = "students"
    ENROLLMENTS = "enrollments"
    GUARDIANS = "guardians"
    STAFF = "staff"
    ADMISSION_ENQUIRIES = "admission_enquiries"
    ADMISSION_APPLICATIONS = "admission_applications"
    ALUMNI = "alumni"

    # Operations
    ATTENDANCE = "attendance_records"
    ATTENDANCE_SESSIONS = "attendance_sessions"
    ASSIGNMENTS = "assignments"
    SUBMISSIONS = "assignment_submissions"
    EXAMS = "exams"
    EXAM_SCHEDULES = "exam_schedules"
    MARKS = "marks"
    GRADE_SCALES = "grade_scales"
    REPORT_CARDS = "report_cards"
    LMS_MATERIALS = "lms_materials"
    CERTIFICATES = "certificates"

    # Finance
    FEE_HEADS = "fee_heads"
    FEE_STRUCTURES = "fee_structures"
    FEE_INVOICES = "fee_invoices"
    PAYMENTS = "payments"
    DISCOUNTS = "discounts"
    EXPENSES = "expenses"
    PAYROLL_RUNS = "payroll_runs"
    PAYSLIPS = "payslips"
    SALARY_STRUCTURES = "salary_structures"

    # HR
    LEAVE_TYPES = "leave_types"
    LEAVE_REQUESTS = "leave_requests"
    STAFF_ATTENDANCE = "staff_attendance"
    APPRAISALS = "appraisals"

    # Facilities
    LIBRARY_ITEMS = "library_items"
    LIBRARY_LOANS = "library_loans"
    TRANSPORT_ROUTES = "transport_routes"
    TRANSPORT_VEHICLES = "transport_vehicles"
    TRANSPORT_STOPS = "transport_stops"
    TRANSPORT_ALLOCATIONS = "transport_allocations"
    HOSTELS = "hostels"
    HOSTEL_ROOMS = "hostel_rooms"
    HOSTEL_ALLOCATIONS = "hostel_allocations"
    INVENTORY_ITEMS = "inventory_items"
    INVENTORY_TXNS = "inventory_transactions"
    VISITORS = "visitors"

    # Communication
    ANNOUNCEMENTS = "announcements"
    EVENTS = "events"
    MESSAGES = "messages"
    THREADS = "message_threads"
    COMPLAINTS = "complaints"

    # Communication delivery
    NOTIFICATION_DELIVERIES = "notification_deliveries"
    MESSAGE_TEMPLATES = "message_templates"

    # Devices & biometrics
    BIOMETRIC_DEVICES = "biometric_devices"
    BIOMETRIC_PUNCHES = "biometric_punches"

    # Live tracking
    VEHICLE_POSITIONS = "vehicle_positions"
    VEHICLE_TRIPS = "vehicle_trips"

    # Online payments
    PAYMENT_ORDERS = "payment_orders"

    # Learning
    ONLINE_CLASSES = "online_classes"

    # AI
    AI_CONVERSATIONS = "ai_conversations"

    # Shared
    DOCUMENTS = "documents"
    COUNTERS = "counters"
