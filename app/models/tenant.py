"""Institution, plan and subscription documents (the SaaS control plane)."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import Field, field_validator

from app.models.base import (
    Address,
    AppModel,
    ContactInfo,
    DBModel,
    FileRef,
    PyObjectId,
    utcnow,
)


class InstitutionType(StrEnum):
    SCHOOL = "school"
    COLLEGE = "college"
    UNIVERSITY = "university"
    INSTITUTE = "institute"
    COACHING = "coaching"


class TenantStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class Deployment(StrEnum):
    SAAS = "saas"
    DEDICATED = "dedicated"


class SubscriptionStatus(StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    LIFETIME = "lifetime"


class BillingCycle(StrEnum):
    MONTHLY = "monthly"
    YEARLY = "yearly"
    LIFETIME = "lifetime"


class Branding(AppModel):
    """Everything an institution can restyle without touching code."""

    logo: FileRef | None = None
    logo_dark: FileRef | None = None
    favicon: FileRef | None = None
    primary_color: str = "#111214"
    accent_color: str = "#FAEE7C"
    login_banner: FileRef | None = None
    tagline: str = ""


class PlanLimitsModel(AppModel):
    max_students: int | None = None
    max_staff: int | None = None
    max_storage_mb: int | None = None
    max_admin_users: int | None = None


class Plan(DBModel):
    """A SaaS price point. Not used by dedicated deployments."""

    key: str
    name: str
    description: str = ""
    price_monthly: float = 0
    price_yearly: float = 0
    currency: str = "INR"
    #: One-off price when an institution buys its own deployment outright.
    price_lifetime: float | None = None
    limits: PlanLimitsModel = Field(default_factory=PlanLimitsModel)
    included_modules: list[str] = Field(default_factory=list)
    trial_days: int = 14
    is_public: bool = True
    is_active: bool = True
    sort_order: int = 0
    highlights: list[str] = Field(default_factory=list)


class Tenant(DBModel):
    """One institution.

    Exists in both deployment modes. On a dedicated server there is exactly one
    of these and its ``deployment`` is ``dedicated``, which switches off every
    plan limit and module gate.
    """

    slug: str
    name: str
    legal_name: str = ""
    institution_type: InstitutionType = InstitutionType.SCHOOL
    status: TenantStatus = TenantStatus.TRIAL
    deployment: Deployment = Deployment.SAAS

    # Contact & identity
    contact: ContactInfo = Field(default_factory=ContactInfo)
    address: Address = Field(default_factory=Address)
    website: str = ""
    registration_number: str = ""
    affiliation_board: str = ""  # CBSE / ICSE / State / UGC / AICTE …
    established_year: int | None = None

    # Look & feel
    branding: Branding = Field(default_factory=Branding)

    # Localisation
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    locale: str = "en-IN"
    academic_year_start_month: int = 4

    # Commercial (SaaS)
    plan_key: str = ""
    subscription_status: SubscriptionStatus = SubscriptionStatus.TRIALING
    subscription_valid_till: date | None = None
    trial_ends_at: date | None = None
    limits: PlanLimitsModel = Field(default_factory=PlanLimitsModel)

    # Dedicated
    license_key: str = ""
    licensed_at: datetime | None = None
    dedicated_domain: str = ""

    # Feature switches
    enabled_modules: list[str] = Field(default_factory=list)
    settings: dict = Field(default_factory=dict)

    # Operational
    current_academic_year_id: PyObjectId | None = None
    onboarding_completed: bool = False
    onboarding_step: str = "profile"
    storage_used_mb: float = 0
    last_active_at: datetime | None = None
    notes: str = ""

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or not all(c.isalnum() or c == "-" for c in v):
            raise ValueError("slug may contain only lowercase letters, numbers and hyphens")
        if v in RESERVED_SLUGS:
            raise ValueError(f"'{v}' is reserved")
        return v


RESERVED_SLUGS = {
    "www", "api", "app", "admin", "platform", "dashboard", "auth", "login",
    "static", "assets", "cdn", "mail", "support", "help", "docs", "status",
    "billing", "scholarly", "system", "public", "internal",
}


class Subscription(DBModel):
    tenant_id: PyObjectId
    plan_key: str
    status: SubscriptionStatus = SubscriptionStatus.TRIALING
    billing_cycle: BillingCycle = BillingCycle.YEARLY
    amount: float = 0
    currency: str = "INR"
    seats_students: int | None = None
    started_at: datetime = Field(default_factory=utcnow)
    current_period_start: date | None = None
    current_period_end: date | None = None
    cancel_at_period_end: bool = False
    cancelled_at: datetime | None = None
    payment_reference: str = ""
    payment_method: str = "manual"
    history: list[dict] = Field(default_factory=list)


class PlatformInvoice(DBModel):
    """What the company bills an institution — distinct from a student's fee
    invoice, which lives inside the institution's own data."""

    tenant_id: PyObjectId
    number: str
    plan_key: str = ""
    billing_cycle: BillingCycle = BillingCycle.YEARLY
    subtotal: float = 0
    tax: float = 0
    total: float = 0
    currency: str = "INR"
    status: str = "draft"  # draft | issued | paid | overdue | void
    issued_on: date | None = None
    due_on: date | None = None
    paid_on: date | None = None
    payment_reference: str = ""
    line_items: list[dict] = Field(default_factory=list)
    notes: str = ""


class PlatformUser(DBModel):
    """Company staff. Never exists on a dedicated deployment."""

    email: str
    full_name: str
    password_hash: str = ""
    role: str = "platform_support"
    is_active: bool = True
    avatar_url: str = ""
    last_login_at: datetime | None = None
