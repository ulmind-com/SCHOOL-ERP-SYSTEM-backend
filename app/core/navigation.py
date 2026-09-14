"""The sidebar, derived rather than hard-coded.

A nav item survives only if the institution has the module switched on *and*
the signed-in user holds the permission. That means a teacher, an accountant
and a parent get three different products out of one build, and turning a
module off in settings removes it everywhere at once.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.context import AuthContext, TenantContext


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str
    icon: str
    permission: str
    module: str = ""
    badge: str = ""
    #: Restrict to certain portals; empty means any.
    portals: tuple[str, ...] = ()
    #: Hide from these portals. A student holds ``invoices:read`` for their own
    #: bill, which is not a reason to show them the bursar's invoice register.
    not_portals: tuple[str, ...] = ()

    @property
    def module_key(self) -> str:
        return self.module or self.permission.split(":", 1)[0]


@dataclass(frozen=True)
class NavGroup:
    label: str
    items: tuple[NavItem, ...]


#: The portals that see their own records rather than the institution's.
FAMILY = ("student", "parent")

NAVIGATION: tuple[NavGroup, ...] = (
    NavGroup("Main Menu", (
        NavItem("dashboard", "Dashboard", "/dashboard", "layout-grid", "dashboard:read"),
        NavItem("students", "Students", "/students", "users", "students:read"),
        # A family's own record, with attendance, marks and files on one screen.
        # The administrative Attendance and Results screens below are built
        # around a class picker and are no use to them — so they get these and
        # not those.
        NavItem("my-record", "My Record", "/portal/me", "user", "attendance:read",
                module="attendance", portals=FAMILY),
        NavItem("attendance", "Attendance", "/attendance", "check-square", "attendance:read",
                not_portals=FAMILY),
        NavItem("timetable", "Timetable", "/timetable", "calendar-clock", "timetable:read"),
        NavItem("homework", "Homework", "/portal/homework", "clipboard-list",
                "assignments:read", module="assignments", portals=FAMILY),
        NavItem("assignments", "Assignments", "/assignments", "clipboard-list",
                "assignments:read", not_portals=FAMILY),
        NavItem("exams", "Exams", "/exams", "file-badge", "exams:read"),
        NavItem("results", "Results", "/results", "trophy", "results:read",
                not_portals=FAMILY),
        NavItem("my-fees", "Fees & Payments", "/portal/fees", "wallet", "invoices:read",
                module="invoices", portals=FAMILY),
        NavItem("assistant", "Assistant", "/assistant", "sparkles", "dashboard:read"),
    )),
    NavGroup("People", (
        NavItem("staff", "Staff & Teachers", "/staff", "briefcase", "staff:read"),
        NavItem("guardians", "Parents", "/guardians", "users-round", "guardians:read"),
        NavItem("admissions", "Admissions", "/admissions", "user-plus", "admissions:read"),
        NavItem("alumni", "Alumni", "/alumni", "award", "alumni:read"),
    )),
    NavGroup("Academics", (
        NavItem("academic-years", "Academic Years", "/academics/years", "calendar-range",
                "academic_years:read"),
        NavItem("classes", "Classes & Sections", "/academics/classes", "layers", "classes:read"),
        NavItem("subjects", "Subjects", "/academics/subjects", "book-open", "subjects:read"),
        NavItem("departments", "Departments", "/academics/departments", "building-2",
                "departments:read"),
        NavItem("programs", "Programs", "/academics/programs", "graduation-cap", "programs:read"),
        NavItem("syllabus", "Syllabus", "/academics/syllabus", "list-checks", "syllabus:read"),
        NavItem("lms", "Learning Material", "/academics/materials", "library-big", "lms:read"),
        NavItem("live-classes", "Live Classes", "/academics/live-classes", "video", "lms:read"),
    )),
    NavGroup("Finance", (
        NavItem("fees", "Fee Structures", "/finance/fees", "receipt", "fees:read"),
        NavItem("invoices", "Invoices", "/finance/invoices", "file-text", "invoices:read",
                not_portals=FAMILY),
        NavItem("payments", "Collection", "/finance/payments", "wallet", "payments:read",
                not_portals=FAMILY),
        NavItem("scholarships", "Scholarships", "/finance/scholarships", "badge-percent",
                "scholarships:read"),
        NavItem("expenses", "Expenses", "/finance/expenses", "trending-down", "expenses:read"),
        NavItem("payroll", "Payroll", "/finance/payroll", "banknote", "payroll:read"),
    )),
    NavGroup("HR", (
        NavItem("leaves", "Leave Requests", "/hr/leaves", "calendar-off", "leaves:read"),
        NavItem("staff-attendance", "Staff Attendance", "/hr/attendance", "fingerprint",
                "staff_attendance:read"),
        NavItem("appraisals", "Appraisals", "/hr/appraisals", "chart-line", "appraisals:read"),
    )),
    NavGroup("Facilities", (
        NavItem("my-library", "Library", "/portal/library", "book-marked", "library:read",
                module="library", portals=FAMILY),
        NavItem("library", "Library", "/facilities/library", "book-marked", "library:read",
                not_portals=FAMILY),
        NavItem("transport", "Transport", "/facilities/transport", "bus", "transport:read"),
        NavItem("hostel", "Hostel", "/facilities/hostel", "bed-double", "hostel:read"),
        NavItem("inventory", "Inventory", "/facilities/inventory", "package", "inventory:read"),
        NavItem("visitors", "Visitors", "/facilities/visitors", "door-open", "visitors:read"),
        NavItem("tracking", "Live Tracking", "/facilities/tracking", "navigation",
                "transport:read", module="transport"),
        NavItem("biometrics", "Biometric Devices", "/facilities/biometrics", "fingerprint",
                "staff_attendance:read", module="staff_attendance"),
    )),
    NavGroup("Communication", (
        NavItem("announcements", "Announcements", "/communication/announcements", "megaphone",
                "announcements:read"),
        NavItem("events", "Events", "/communication/events", "calendar-days", "events:read"),
        NavItem("messages", "Messages", "/communication/messages", "message-square",
                "messages:read"),
        NavItem("complaints", "Helpdesk", "/communication/complaints", "life-buoy",
                "complaints:read"),
    )),
    NavGroup("Insight", (
        NavItem("reports", "Reports", "/reports", "bar-chart-3", "reports:read"),
        NavItem("documents", "Documents", "/documents", "folder", "documents:read"),
        NavItem("certificates", "Certificates", "/certificates", "id-card", "certificates:read"),
        NavItem("audit", "Audit Log", "/settings/audit", "scroll-text", "audit:read"),
    )),
    NavGroup("Administration", (
        NavItem("users", "Users & Access", "/settings/users", "user-cog", "users:read"),
        NavItem("roles", "Roles", "/settings/roles", "shield", "roles:read"),
        NavItem("settings", "Settings", "/settings", "settings", "settings:read"),
    )),
)

PLATFORM_NAVIGATION: tuple[NavGroup, ...] = (
    NavGroup("Platform", (
        NavItem("overview", "Overview", "/platform", "layout-grid", "metrics:read"),
        NavItem("tenants", "Institutions", "/platform/institutions", "building-2", "tenants:read"),
        NavItem("plans", "Plans", "/platform/plans", "layers", "plans:read"),
        NavItem("subscriptions", "Subscriptions", "/platform/subscriptions", "refresh-cw",
                "subscriptions:read"),
        NavItem("billing", "Billing", "/platform/billing", "receipt", "invoices:read"),
        NavItem("team", "Platform Team", "/platform/team", "user-cog", "platform_users:read"),
        NavItem("audit", "Activity", "/platform/activity", "scroll-text", "audit:read"),
    )),
)


def build_navigation(auth: AuthContext, tenant: TenantContext | None) -> list[dict]:
    source = PLATFORM_NAVIGATION if auth.is_platform else NAVIGATION
    out: list[dict] = []
    for group in source:
        items = [
            {
                "key": item.key,
                "label": item.label,
                "href": item.href,
                "icon": item.icon,
                "module": item.module_key,
            }
            for item in group.items
            if auth.can(item.permission)
            and (not item.portals or auth.portal in item.portals)
            and auth.portal not in item.not_portals
            and (auth.is_platform or tenant is None or tenant.module_enabled(item.module_key))
        ]
        if items:
            out.append({"label": group.label, "items": items})
    return out
