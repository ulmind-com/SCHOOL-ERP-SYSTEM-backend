"""Default SaaS plans and the module bundles they unlock.

These are seeded once and then owned by the platform console — editing a plan
there does not require a deploy.
"""

from __future__ import annotations

from app.core.permissions import MODULES

ALL_MODULE_KEYS = [m.key for m in MODULES]

ESSENTIALS = [
    "students", "guardians", "staff", "attendance", "assignments", "exams",
    "results", "announcements", "events", "messages", "reports", "documents",
    "admissions", "departments", "programs", "timetable",
]

GROWTH = ESSENTIALS + [
    "fees", "invoices", "payments", "scholarships", "syllabus", "lms",
    "certificates", "complaints", "leaves", "staff_attendance", "library",
]

SCALE = GROWTH + [
    "expenses", "payroll", "transport", "hostel", "inventory", "visitors",
    "alumni", "appraisals",
]

DEFAULT_PLANS: list[dict] = [
    {
        "key": "trial",
        "name": "Free Trial",
        "description": "Everything in Growth for 14 days. No card required.",
        "price_monthly": 0,
        "price_yearly": 0,
        "trial_days": 14,
        "is_public": False,
        "sort_order": 0,
        "limits": {"max_students": 100, "max_staff": 25, "max_storage_mb": 1024,
                   "max_admin_users": 5},
        "included_modules": GROWTH,
        "highlights": ["14 days", "Up to 100 students", "Guided setup"],
    },
    {
        "key": "essentials",
        "name": "Essentials",
        "description": "For single-campus schools getting off spreadsheets.",
        "price_monthly": 2999,
        "price_yearly": 29990,
        "trial_days": 14,
        "sort_order": 1,
        "limits": {"max_students": 500, "max_staff": 60, "max_storage_mb": 5120,
                   "max_admin_users": 10},
        "included_modules": ESSENTIALS,
        "highlights": ["Up to 500 students", "Attendance & exams",
                       "Parent and student portals", "Email support"],
    },
    {
        "key": "growth",
        "name": "Growth",
        "description": "Adds the full finance stack and learning material.",
        "price_monthly": 5999,
        "price_yearly": 59990,
        "trial_days": 14,
        "sort_order": 2,
        "limits": {"max_students": 2000, "max_staff": 250, "max_storage_mb": 25600,
                   "max_admin_users": 30},
        "included_modules": GROWTH,
        "highlights": ["Up to 2,000 students", "Fees, invoicing & collection",
                       "Library & leave management", "Priority support"],
    },
    {
        "key": "scale",
        "name": "Scale",
        "description": "Colleges and universities running transport, hostel and payroll.",
        "price_monthly": 11999,
        "price_yearly": 119990,
        "trial_days": 14,
        "sort_order": 3,
        "limits": {"max_students": 10000, "max_staff": 1200, "max_storage_mb": 102400,
                   "max_admin_users": 100},
        "included_modules": SCALE,
        "highlights": ["Up to 10,000 students", "Payroll & expenses",
                       "Transport, hostel & inventory", "Dedicated success manager"],
    },
    {
        "key": "enterprise",
        "name": "Enterprise",
        "description": "Multi-campus universities. Unlimited everything, custom SLA.",
        "price_monthly": 0,
        "price_yearly": 0,
        "trial_days": 0,
        "sort_order": 4,
        "limits": {},
        "included_modules": list(ALL_MODULE_KEYS),
        "highlights": ["Unlimited students & staff", "Every module",
                       "Custom integrations", "99.9% uptime SLA"],
    },
    {
        "key": "lifetime",
        "name": "Dedicated / Lifetime",
        "description": (
            "The institution runs its own deployment with its own database. "
            "One-off licence, no limits, full control of every module and setting."
        ),
        "price_monthly": 0,
        "price_yearly": 0,
        "price_lifetime": 499000,
        "trial_days": 0,
        "is_public": True,
        "sort_order": 5,
        "limits": {},
        "included_modules": list(ALL_MODULE_KEYS),
        "highlights": ["Your own database", "Your own domain & branding",
                       "Every module unlocked", "Own the data outright"],
    },
]

PLANS_BY_KEY = {p["key"]: p for p in DEFAULT_PLANS}
