"""Institution-side identity: users, roles and sessions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import EmailStr, Field

from app.models.base import AppModel, PyObjectId, TenantDocument, utcnow


class UserStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Portal(StrEnum):
    ADMIN = "admin"
    TEACHER = "teacher"
    STUDENT = "student"
    PARENT = "parent"
    FINANCE = "finance"


class Role(TenantDocument):
    """Roles are per-institution documents, so a college can define
    'Exam Controller' without us shipping a release."""

    key: str
    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    portal: Portal = Portal.ADMIN
    #: Seeded roles cannot be deleted, only edited (except the owner role).
    is_system: bool = False
    is_owner: bool = False
    #: Set the first time an institution edits a seeded role. After that we stop
    #: reconciling it with the preset — their decision outranks ours.
    is_customised: bool = False
    user_count: int = 0


class User(TenantDocument):
    email: EmailStr
    full_name: str
    password_hash: str = ""
    phone: str = ""
    avatar_url: str = ""
    role_ids: list[PyObjectId] = Field(default_factory=list)
    status: UserStatus = UserStatus.INVITED
    is_active: bool = True

    # Links to the person record behind this login, when there is one.
    student_id: PyObjectId | None = None
    staff_id: PyObjectId | None = None
    guardian_id: PyObjectId | None = None

    must_change_password: bool = False
    last_login_at: datetime | None = None
    last_login_ip: str = ""
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    invited_at: datetime | None = None
    invite_token_hash: str = ""
    reset_token_hash: str = ""
    reset_requested_at: datetime | None = None
    preferences: dict = Field(default_factory=dict)


class Session(AppModel):
    """A live refresh token. Stored hashed so a database read cannot mint one."""

    id: PyObjectId | None = Field(default=None, alias="_id")
    tenant_id: PyObjectId | None = None
    user_id: PyObjectId
    scope: str = "tenant"
    token_hash: str
    user_agent: str = ""
    ip: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    revoked_at: datetime | None = None


class AuditLog(TenantDocument):
    actor_id: PyObjectId | None = None
    actor_name: str = ""
    action: str = ""             # e.g. "student.create"
    entity_type: str = ""
    entity_id: PyObjectId | None = None
    entity_label: str = ""
    changes: dict = Field(default_factory=dict)
    ip: str = ""
    user_agent: str = ""
