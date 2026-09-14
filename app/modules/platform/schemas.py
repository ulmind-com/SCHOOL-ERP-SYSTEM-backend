from __future__ import annotations

from datetime import date

from pydantic import EmailStr, Field

from app.models.base import AppModel


class TenantCreateRequest(AppModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str | None = Field(default=None, max_length=40)
    institution_type: str = "school"
    owner_email: EmailStr
    owner_name: str = ""
    owner_password: str | None = None
    plan_key: str = "trial"
    deployment: str = "saas"
    phone: str = ""
    city: str = ""
    state: str = ""
    country: str = "India"
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    academic_year_start_month: int = Field(default=4, ge=1, le=12)
    enabled_modules: list[str] | None = None
    notes: str = ""


class TenantUpdateRequest(AppModel):
    name: str | None = None
    legal_name: str | None = None
    institution_type: str | None = None
    website: str | None = None
    affiliation_board: str | None = None
    registration_number: str | None = None
    timezone: str | None = None
    currency: str | None = None
    locale: str | None = None
    enabled_modules: list[str] | None = None
    dedicated_domain: str | None = None
    notes: str | None = None
    contact: dict | None = None
    address: dict | None = None
    settings: dict | None = None


class TenantStatusRequest(AppModel):
    status: str  # active | suspended | cancelled
    reason: str = ""


class SubscriptionChangeRequest(AppModel):
    plan_key: str
    billing_cycle: str = "yearly"
    amount: float | None = None
    valid_till: date | None = None
    payment_reference: str = ""
    note: str = ""


class PlanUpsertRequest(AppModel):
    key: str
    name: str
    description: str = ""
    price_monthly: float = 0
    price_yearly: float = 0
    price_lifetime: float | None = None
    currency: str = "INR"
    trial_days: int = 14
    limits: dict = Field(default_factory=dict)
    included_modules: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    is_public: bool = True
    is_active: bool = True
    sort_order: int = 0


class SignupRequest(AppModel):
    """Self-serve trial signup from the marketing site."""

    institution_name: str = Field(min_length=2, max_length=160)
    institution_type: str = "school"
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str
    phone: str = ""
    city: str = ""
    preferred_slug: str | None = None
    plan_key: str = "trial"


class ImpersonateRequest(AppModel):
    reason: str = Field(min_length=4, max_length=300)


class PlatformMetrics(AppModel):
    institutions_total: int
    institutions_active: int
    institutions_trial: int
    institutions_dedicated: int
    institutions_suspended: int
    students_total: int
    staff_total: int
    mrr: float
    arr: float
    expiring_in_30_days: int
    signups_this_month: int
    by_plan: list[dict]
    by_type: list[dict]
    recent_signups: list[dict]
