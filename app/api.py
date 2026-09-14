"""Router assembly.

The platform console is mounted only on SaaS deployments. A dedicated
institution's server simply does not carry those routes, which is a stronger
guarantee than hiding the menu in the UI.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings
from app.modules.ai.router import router as ai_router
from app.modules.attendance.router import router as attendance_router
from app.modules.auth.router import router as auth_router
from app.modules.biometrics.router import device_router as biometric_device_router
from app.modules.biometrics.router import router as biometrics_router
from app.modules.communication.router import broadcast_router
from app.modules.communication.router import router as messages_router
from app.modules.dashboard.router import router as dashboard_router
from app.modules.documents.router import router as documents_router
from app.modules.exams.router import router as exams_router
from app.modules.fees.router import router as fees_router
from app.modules.library.router import router as library_router
from app.modules.lms.router import homework_router
from app.modules.lms.router import router as live_classes_router
from app.modules.payments.router import router as online_payments_router
from app.modules.payroll.router import router as payroll_router
from app.modules.people.router import router as people_router
from app.modules.platform.router import public_router
from app.modules.platform.router import router as platform_router
from app.modules.portal.router import router as portal_router
from app.modules.printing.router import router as printing_router
from app.modules.registry import build_registry_router
from app.modules.reports.router import router as reports_router
from app.modules.settings.router import router as settings_router
from app.modules.tracking.router import router as tracking_router
from app.modules.users.router import router as users_router

api_router = APIRouter()

# ── Always mounted ────────────────────────────────────────────────────────
api_router.include_router(auth_router)
api_router.include_router(build_registry_router())
api_router.include_router(people_router)
api_router.include_router(attendance_router)
api_router.include_router(fees_router)
api_router.include_router(documents_router)
api_router.include_router(exams_router)
api_router.include_router(settings_router)
api_router.include_router(dashboard_router)
api_router.include_router(messages_router)
api_router.include_router(broadcast_router)
api_router.include_router(reports_router)
api_router.include_router(biometrics_router)
api_router.include_router(tracking_router)
api_router.include_router(online_payments_router)
api_router.include_router(ai_router)
api_router.include_router(library_router)
api_router.include_router(payroll_router)
api_router.include_router(printing_router)
api_router.include_router(live_classes_router)
api_router.include_router(homework_router)
api_router.include_router(users_router)
api_router.include_router(portal_router)

# ── SaaS-only ─────────────────────────────────────────────────────────────
if settings.is_saas:
    api_router.include_router(platform_router)
    api_router.include_router(public_router)


def build_api_router() -> APIRouter:
    return api_router


def build_device_router() -> APIRouter:
    """Routes spoken by hardware, mounted at the application root.

    ZKTeco firmware has a fixed `/iclock/...` server path on many models, so
    these cannot live under the versioned API prefix.
    """
    root = APIRouter()
    root.include_router(biometric_device_router)
    return root
