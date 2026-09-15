from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field

from app.models.base import AppModel


class LoginRequest(AppModel):
    #: Email address or phone number. Parents in particular are far likelier to
    #: remember the number the school already has on file than an address the
    #: office invented for them, so both are accepted.
    email: str
    password: str
    #: Optional when the institution is already implied by subdomain, header or
    #: because the email exists at exactly one institution.
    institution: str | None = None
    remember_me: bool = True


class InstitutionChoice(AppModel):
    slug: str
    name: str
    logo_url: str = ""
    institution_type: str = "school"


class TokenPair(AppModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class SessionUser(AppModel):
    id: str
    email: str
    full_name: str
    avatar_url: str = ""
    scope: str = "tenant"
    portal: str = "admin"
    roles: list[str] = Field(default_factory=list)
    role_keys: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    is_owner: bool = False
    student_id: str | None = None
    staff_id: str | None = None
    guardian_id: str | None = None
    last_login_at: datetime | None = None


class SessionInstitution(AppModel):
    id: str
    slug: str
    name: str
    institution_type: str
    deployment: str
    status: str
    subscription_status: str
    subscription_valid_till: str | None = None
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    locale: str = "en-IN"
    branding: dict = Field(default_factory=dict)
    enabled_modules: list[str] = Field(default_factory=list)
    limits: dict = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
    current_academic_year: dict | None = None
    #: Every year this institution has run, newest first, so staff can move
    #: between them without a second request.
    academic_years: list[dict] = Field(default_factory=list)
    active_academic_year_id: str | None = None
    #: Families are pinned to the current year; only staff get the switcher.
    can_switch_academic_year: bool = False
    onboarding_completed: bool = True


class LoginResponse(AppModel):
    tokens: TokenPair
    user: SessionUser
    institution: SessionInstitution | None = None
    must_change_password: bool = False


class RefreshRequest(AppModel):
    refresh_token: str


class ChangePasswordRequest(AppModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(AppModel):
    email: EmailStr
    institution: str | None = None


class ResetPasswordRequest(AppModel):
    token: str
    new_password: str


class AcceptInviteRequest(AppModel):
    token: str
    password: str
    full_name: str | None = None


class MeResponse(AppModel):
    user: SessionUser
    institution: SessionInstitution | None = None
    navigation: list[dict] = Field(default_factory=list)
    deployment_mode: str = "saas"

