"""Permission catalogue and the role presets built on top of it.

A permission is ``"<module>:<action>"``. Roles are stored per-tenant as
documents so an institution can invent its own ("Exam Controller",
"Front Desk") — the presets below are only what we seed on provisioning.

``*`` is a wildcard: ``"*"`` grants everything, ``"students:*"`` grants every
action on students. ``has_permission`` understands both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Actions ───────────────────────────────────────────────────────────────
READ = "read"
CREATE = "create"
UPDATE = "update"
DELETE = "delete"
EXPORT = "export"
APPROVE = "approve"
PUBLISH = "publish"
COLLECT = "collect"

CRUD = (READ, CREATE, UPDATE, DELETE)


#: The kinds of institution this product serves. Two families, really: places
#: that teach children in classes, and places that teach adults in programmes.
SCHOOL_LIKE = ("school", "coaching")
HIGHER_ED = ("college", "university", "institute")


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    group: str
    actions: tuple[str, ...] = CRUD
    #: Modules an institution can switch off without breaking the core.
    optional: bool = True
    icon: str = "square"
    #: The institution types this module means anything to. Empty means every
    #: type. A school has no Faculties and a university has no Class 7, and
    #: showing either one the other's vocabulary is how an ERP starts feeling
    #: like it was built for somebody else.
    for_types: tuple[str, ...] = ()

    @property
    def permissions(self) -> list[str]:
        return [f"{self.key}:{a}" for a in self.actions]

    def suits(self, institution_type: str) -> bool:
        return not self.for_types or institution_type in self.for_types


# ── The catalogue ─────────────────────────────────────────────────────────
MODULES: tuple[Module, ...] = (
    # Core / always on
    Module("dashboard", "Dashboard", "Core", (READ,), optional=False, icon="layout-grid"),
    Module("users", "Users & Access", "Core", CRUD, optional=False, icon="user-cog"),
    Module("roles", "Roles & Permissions", "Core", CRUD, optional=False, icon="shield"),
    Module("settings", "Institution Settings", "Core", (READ, UPDATE), optional=False, icon="settings"),
    Module("audit", "Audit Log", "Core", (READ, EXPORT), optional=False, icon="scroll-text"),
    # Academics
    Module("academic_years", "Academic Years", "Academics", CRUD, optional=False, icon="calendar-range"),
    Module("departments", "Departments & Faculties", "Academics", CRUD, icon="building-2",
           for_types=HIGHER_ED),
    Module("programs", "Programs & Courses", "Academics", CRUD, icon="graduation-cap",
           for_types=HIGHER_ED),
    Module("classes", "Classes & Sections", "Academics", CRUD, optional=False, icon="layers"),
    Module("subjects", "Subjects", "Academics", CRUD, optional=False, icon="book-open"),
    Module("timetable", "Timetable", "Academics", (*CRUD, PUBLISH), icon="calendar-clock"),
    Module("syllabus", "Syllabus & Lesson Plans", "Academics", CRUD, icon="list-checks"),
    # People
    Module("students", "Students", "People", (*CRUD, EXPORT), optional=False, icon="users"),
    Module("guardians", "Parents & Guardians", "People", (*CRUD, EXPORT), icon="users-round"),
    Module("staff", "Staff & Teachers", "People", (*CRUD, EXPORT), optional=False, icon="briefcase"),
    Module("admissions", "Admissions", "People", (*CRUD, APPROVE, EXPORT), icon="user-plus"),
    Module("alumni", "Alumni", "People", CRUD, icon="award"),
    # Operations
    Module("attendance", "Attendance", "Operations", (*CRUD, EXPORT), optional=False, icon="check-square"),
    Module("assignments", "Assignments & Homework", "Operations", (*CRUD, PUBLISH), icon="clipboard-list"),
    Module("exams", "Exams & Grading", "Operations", (*CRUD, PUBLISH, EXPORT), icon="file-badge"),
    Module("results", "Results & Report Cards", "Operations", (READ, PUBLISH, EXPORT), icon="trophy"),
    Module("lms", "Learning Materials", "Operations", (*CRUD, PUBLISH), icon="library-big"),
    Module("certificates", "Certificates & ID Cards", "Operations", (READ, CREATE, EXPORT), icon="id-card"),
    # Finance
    Module("fees", "Fee Structures", "Finance", CRUD, icon="receipt"),
    Module("invoices", "Fee Invoices", "Finance", (*CRUD, EXPORT), icon="file-text"),
    Module("payments", "Payments & Collection", "Finance", (READ, COLLECT, CREATE, EXPORT), icon="wallet"),
    Module("expenses", "Expenses", "Finance", (*CRUD, APPROVE), icon="trending-down"),
    Module("payroll", "Payroll", "Finance", (*CRUD, APPROVE, EXPORT), icon="banknote"),
    Module("scholarships", "Scholarships & Discounts", "Finance", (*CRUD, APPROVE), icon="badge-percent"),
    # HR
    Module("leaves", "Leave Management", "HR", (*CRUD, APPROVE), icon="calendar-off"),
    Module("staff_attendance", "Staff Attendance", "HR", (*CRUD, EXPORT), icon="fingerprint"),
    Module("appraisals", "Appraisals", "HR", (*CRUD, APPROVE), icon="chart-line"),
    # Facilities
    Module("library", "Library", "Facilities", (*CRUD, EXPORT), icon="book-marked"),
    Module("transport", "Transport", "Facilities", CRUD, icon="bus"),
    Module("hostel", "Hostel", "Facilities", CRUD, icon="bed-double"),
    Module("inventory", "Inventory & Assets", "Facilities", CRUD, icon="package"),
    Module("visitors", "Visitor & Gate Pass", "Facilities", CRUD, icon="door-open"),
    # Communication
    Module("announcements", "Announcements", "Communication", (*CRUD, PUBLISH), icon="megaphone"),
    Module("events", "Events & Calendar", "Communication", (*CRUD, PUBLISH), icon="calendar-days"),
    Module("messages", "Messaging", "Communication", (READ, CREATE), icon="message-square"),
    Module("complaints", "Complaints & Helpdesk", "Communication", (*CRUD, APPROVE), icon="life-buoy"),
    # Insight
    Module("reports", "Reports & Analytics", "Insight", (READ, EXPORT), icon="bar-chart-3"),
    Module("documents", "Document Vault", "Insight", CRUD, icon="folder"),
)

MODULES_BY_KEY: dict[str, Module] = {m.key: m for m in MODULES}
ALL_PERMISSIONS: list[str] = [p for m in MODULES for p in m.permissions]
ALL_MODULE_KEYS: list[str] = [m.key for m in MODULES]
OPTIONAL_MODULE_KEYS: list[str] = [m.key for m in MODULES if m.optional]
CORE_MODULE_KEYS: list[str] = [m.key for m in MODULES if not m.optional]


def modules_for_type(institution_type: str) -> set[str]:
    """Every module key that makes sense for this kind of institution."""
    return {m.key for m in MODULES if m.suits(institution_type)}


def module_group_tree(institution_type: str = "") -> list[dict]:
    """Catalogue grouped for the UI's module/permission pickers.

    Pass an institution type to leave out what that kind of institution does
    not have — a school administrator should never be offered a Faculties
    toggle, let alone find one switched on.
    """
    groups: dict[str, list[dict]] = {}
    for m in MODULES:
        if institution_type and not m.suits(institution_type):
            continue
        groups.setdefault(m.group, []).append(
            {
                "key": m.key,
                "label": m.label,
                "icon": m.icon,
                "optional": m.optional,
                "actions": list(m.actions),
                "permissions": m.permissions,
            }
        )
    return [{"group": g, "modules": mods} for g, mods in groups.items()]


# ── Wildcard-aware checking ───────────────────────────────────────────────
def expand(permissions: list[str]) -> set[str]:
    """Expand wildcards into concrete permissions."""
    out: set[str] = set()
    for p in permissions:
        if p == "*":
            return set(ALL_PERMISSIONS)
        if p.endswith(":*"):
            mod = MODULES_BY_KEY.get(p[:-2])
            if mod:
                out.update(mod.permissions)
        else:
            out.add(p)
    return out


def has_permission(granted: list[str] | set[str], required: str) -> bool:
    if not granted:
        return False
    granted = set(granted)
    if "*" in granted or required in granted:
        return True
    module = required.split(":", 1)[0]
    return f"{module}:*" in granted


# ── Role presets seeded into every new institution ────────────────────────
@dataclass(frozen=True)
class RolePreset:
    key: str
    name: str
    description: str
    permissions: list[str] = field(default_factory=list)
    #: The institution owner's role — cannot be deleted or stripped.
    is_owner: bool = False
    #: Portal this role lands in after login.
    portal: str = "admin"


def _all(*modules: str) -> list[str]:
    return [f"{m}:*" for m in modules]


def _read(*modules: str) -> list[str]:
    return [f"{m}:read" for m in modules]


ROLE_PRESETS: tuple[RolePreset, ...] = (
    RolePreset(
        "super_admin",
        "Super Admin",
        "Owns the institution. Full control over every module, user and setting.",
        ["*"],
        is_owner=True,
    ),
    RolePreset(
        "admin",
        "Administrator",
        "Day-to-day administration across academics, people and operations.",
        [
            *_all(
                "academic_years", "departments", "programs", "classes", "subjects",
                "timetable", "syllabus", "students", "guardians", "staff", "admissions",
                "attendance", "assignments", "exams", "lms", "certificates",
                "announcements", "events", "messages", "complaints", "documents",
                "library", "transport", "hostel", "inventory", "visitors", "alumni",
            ),
            *_read("dashboard", "users", "roles", "settings", "reports", "results",
                   "fees", "invoices", "payments"),
            "reports:export", "results:publish", "results:export", "users:create",
            "users:update",
        ],
    ),
    RolePreset(
        "principal",
        "Principal / Director",
        "Oversight of the whole institution with approval authority.",
        [
            *_read(
                "dashboard", "students", "guardians", "staff", "admissions", "attendance",
                "exams", "results", "fees", "invoices", "payments", "expenses", "payroll",
                "reports", "classes", "subjects", "timetable", "departments", "programs",
                "library", "transport", "hostel", "leaves", "staff_attendance",
            ),
            "reports:export", "results:publish", "announcements:*", "events:*",
            "leaves:approve", "admissions:approve", "expenses:approve",
            "complaints:*", "appraisals:*",
        ],
    ),
    RolePreset(
        "hod",
        "Head of Department",
        "Manages a department's staff, subjects and results.",
        [
            *_read("dashboard", "students", "staff", "classes", "subjects", "timetable",
                   "attendance", "exams", "results", "reports"),
            "subjects:*", "syllabus:*", "assignments:*", "exams:create", "exams:update",
            "leaves:approve", "announcements:create", "reports:export",
        ],
    ),
    RolePreset(
        "teacher",
        "Teacher",
        "Takes attendance, sets assignments, enters marks for assigned classes.",
        [
            *_read("dashboard", "students", "guardians", "classes", "subjects",
                   "timetable", "syllabus", "results", "events", "announcements"),
            "attendance:create", "attendance:read", "attendance:update",
            "assignments:*", "lms:*", "exams:read", "exams:update",
            "messages:*", "leaves:create", "leaves:read", "documents:read",
        ],
        portal="teacher",
    ),
    RolePreset(
        "accountant",
        "Accountant",
        "Fees, invoicing, collection, expenses and payroll.",
        [
            *_all("fees", "invoices", "payments", "expenses", "scholarships"),
            *_read("dashboard", "students", "guardians", "staff", "classes", "reports"),
            "payroll:*", "reports:export",
        ],
        portal="finance",
    ),
    RolePreset(
        "librarian", "Librarian", "Catalogue, issue and return of library items.",
        [*_all("library"), *_read("dashboard", "students", "staff", "classes"), "messages:create"],
        portal="admin",
    ),
    RolePreset(
        "transport_manager", "Transport Manager", "Routes, vehicles, drivers and stops.",
        [*_all("transport"), *_read("dashboard", "students", "staff"), "messages:create"],
        portal="admin",
    ),
    RolePreset(
        "hostel_warden", "Hostel Warden", "Rooms, allocations and hostel attendance.",
        [*_all("hostel"), *_read("dashboard", "students", "guardians"), "messages:create",
         "complaints:read", "complaints:update"],
        portal="admin",
    ),
    RolePreset(
        "receptionist", "Front Desk", "Enquiries, visitors and admission intake.",
        [*_all("visitors"), "admissions:create", "admissions:read", "admissions:update",
         *_read("dashboard", "students", "guardians", "staff", "classes", "events"),
         "messages:create", "complaints:create", "complaints:read"],
        portal="admin",
    ),
    RolePreset(
        "student", "Student", "Self-service portal.",
        [*_read("dashboard", "timetable", "subjects", "syllabus", "attendance",
                "assignments", "results", "lms", "events", "announcements", "invoices",
                "library", "documents"),
         # Deliberately no assignments:update: that is what grades a submission,
         # and it also opened the edit drawer on the assignment itself. Handing
         # in homework goes through /homework/submit, which needs nothing more
         # than being signed in.
         "messages:*", "complaints:create", "complaints:read",
         "leaves:create", "leaves:read"],
        portal="student",
    ),
    RolePreset(
        "parent", "Parent / Guardian", "Follows their children's progress and dues.",
        [*_read("dashboard", "attendance", "assignments", "results", "timetable",
                "events", "announcements", "invoices", "payments", "documents"),
         "messages:*", "complaints:create", "complaints:read", "leaves:create"],
        portal="parent",
    ),
)

ROLE_PRESETS_BY_KEY: dict[str, RolePreset] = {r.key: r for r in ROLE_PRESETS}

# ── Platform-level roles (SaaS deployment only, outside any tenant) ────────
PLATFORM_OWNER = "platform_owner"
PLATFORM_ADMIN = "platform_admin"
PLATFORM_SUPPORT = "platform_support"
PLATFORM_ROLES = (PLATFORM_OWNER, PLATFORM_ADMIN, PLATFORM_SUPPORT)

PLATFORM_PERMISSIONS: dict[str, list[str]] = {
    PLATFORM_OWNER: ["*"],
    PLATFORM_ADMIN: [
        "tenants:*", "plans:*", "subscriptions:*", "invoices:*",
        "platform_users:read", "metrics:read", "audit:read",
    ],
    PLATFORM_SUPPORT: [
        "tenants:read", "subscriptions:read", "invoices:read", "metrics:read",
        "tenants:impersonate",
    ],
}
