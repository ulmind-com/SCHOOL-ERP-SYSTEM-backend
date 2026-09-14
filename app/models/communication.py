"""Announcements, events, messaging, helpdesk and the document vault."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.base import AppModel, FileRef, PyObjectId, TenantDocument


class Audience(AppModel):
    """Who a notice reaches. Empty lists mean 'everyone in that dimension'."""

    everyone: bool = False
    roles: list[str] = Field(default_factory=list)          # role keys
    class_ids: list[str] = Field(default_factory=list)
    section_ids: list[str] = Field(default_factory=list)
    department_ids: list[str] = Field(default_factory=list)
    user_ids: list[str] = Field(default_factory=list)


class Announcement(TenantDocument):
    title: str
    body: str = ""
    summary: str = ""
    category: str = "general"       # general | academic | exam | holiday | fee | urgent
    priority: str = "normal"        # low | normal | high | urgent
    audience: Audience = Field(default_factory=Audience)
    attachments: list[FileRef] = Field(default_factory=list)
    cover: FileRef | None = None
    status: str = "draft"           # draft | published | archived
    published_at: datetime | None = None
    expires_at: datetime | None = None
    published_by: PyObjectId | None = None
    pin_to_top: bool = False
    read_by: list[PyObjectId] = Field(default_factory=list)
    view_count: int = 0


class Event(TenantDocument):
    title: str
    description: str = ""
    category: str = "general"       # exam | holiday | sports | cultural | meeting | ptm
    start_at: datetime
    end_at: datetime | None = None
    all_day: bool = False
    location: str = ""
    audience: Audience = Field(default_factory=Audience)
    cover: FileRef | None = None
    attachments: list[FileRef] = Field(default_factory=list)
    organiser_staff_id: PyObjectId | None = None
    is_holiday: bool = False
    status: str = "scheduled"       # scheduled | ongoing | completed | cancelled
    rsvp_enabled: bool = False
    attendee_ids: list[PyObjectId] = Field(default_factory=list)
    colour: str = ""


class MessageThread(TenantDocument):
    subject: str = ""
    participant_ids: list[PyObjectId] = Field(default_factory=list)
    participant_names: list[str] = Field(default_factory=list)
    type: str = "direct"            # direct | group | broadcast
    context_type: str = ""          # student | class | complaint
    context_id: PyObjectId | None = None
    last_message: str = ""
    last_message_at: datetime | None = None
    unread_by: list[PyObjectId] = Field(default_factory=list)
    is_archived: bool = False


class Message(TenantDocument):
    thread_id: PyObjectId
    sender_id: PyObjectId
    sender_name: str = ""
    body: str = ""
    attachments: list[FileRef] = Field(default_factory=list)
    read_by: list[PyObjectId] = Field(default_factory=list)
    edited_at: datetime | None = None


class Complaint(TenantDocument):
    ticket_number: str = ""
    title: str
    description: str = ""
    category: str = "general"       # academic | facility | transport | fee | discipline
    priority: str = "normal"
    raised_by: PyObjectId | None = None
    raised_by_name: str = ""
    raised_by_type: str = "student"  # student | parent | staff
    related_student_id: PyObjectId | None = None
    assigned_to: PyObjectId | None = None
    status: str = "open"            # open | in_progress | resolved | closed | reopened
    attachments: list[FileRef] = Field(default_factory=list)
    responses: list[dict] = Field(default_factory=list)
    resolved_at: datetime | None = None
    resolved_by: PyObjectId | None = None
    resolution: str = ""
    satisfaction_rating: int | None = None


class Document(TenantDocument):
    """The institution's file vault. Every upload lands here with an owner, so
    a student's documents follow the student and nothing is orphaned."""

    name: str
    description: str = ""
    category: str = "general"       # admission | certificate | id_proof | report | policy
    owner_type: str = "institution"  # institution | student | staff | guardian | class
    owner_id: PyObjectId | None = None
    file: FileRef
    tags: list[str] = Field(default_factory=list)
    is_private: bool = True
    visible_to_roles: list[str] = Field(default_factory=list)
    expires_on: datetime | None = None
    uploaded_by: PyObjectId | None = None


class Notification(TenantDocument):
    user_id: PyObjectId
    title: str
    body: str = ""
    type: str = "info"              # info | success | warning | alert
    category: str = "general"
    link: str = ""
    entity_type: str = ""
    entity_id: PyObjectId | None = None
    read_at: datetime | None = None
