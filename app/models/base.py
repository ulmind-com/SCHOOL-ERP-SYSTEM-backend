"""Shared Pydantic building blocks for every document and API schema."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Generic, TypeVar

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_serializer,
)


def _validate_object_id(v: Any) -> Any:
    if v is None or isinstance(v, ObjectId):
        return v
    if isinstance(v, str):
        try:
            return ObjectId(v)
        except (InvalidId, TypeError) as exc:
            raise ValueError(f"'{v}' is not a valid id") from exc
    raise ValueError(f"cannot interpret {type(v).__name__} as an id")


#: An ObjectId in the model, a plain string on the wire.
#
# ``when_used="json"`` matters: without it the serializer also fires for
# ``model_dump()``, which is what we hand to Mongo — every foreign key would be
# written as a string and then never match a query built from an ObjectId.
PyObjectId = Annotated[
    ObjectId,
    BeforeValidator(_validate_object_id),
    PlainSerializer(lambda v: str(v), return_type=str, when_used="json"),
    # ObjectId is an arbitrary class as far as Pydantic is concerned, so without
    # this it has no JSON schema at all: every request body containing an id
    # quietly dropped out of the OpenAPI document, leaving a dangling $ref.
    WithJsonSchema({"type": "string", "examples": ["65f0c3a2e1b4d2a7c8f01234"]}),
]


def utcnow() -> datetime:
    return datetime.now(UTC)


def oid(value: str | ObjectId | None) -> ObjectId | None:
    """Best-effort coercion used at the repository boundary."""
    if value is None:
        return None
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


class AppModel(BaseModel):
    """Base for request/response schemas."""

    # No `json_encoders`: PyObjectId carries its own serializer and pydantic
    # handles datetimes natively. Declaring them here only re-adds a deprecated
    # code path.
    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )


class DBModel(AppModel):
    """Base for documents that live in MongoDB."""

    id: PyObjectId | None = Field(default=None, alias="_id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by: PyObjectId | None = None
    updated_by: PyObjectId | None = None
    is_deleted: bool = False
    deleted_at: datetime | None = None

    def to_mongo(self, *, exclude_none: bool = False) -> dict[str, Any]:
        data = bsonify(self.model_dump(by_alias=True, exclude_none=exclude_none))
        if data.get("_id") is None:
            data.pop("_id", None)
        return data


def to_datetime(value: date | datetime | None) -> datetime | None:
    """BSON stores datetimes, not dates. Promote to UTC midnight so a plain
    calendar date round-trips without surprises."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def bsonify(value: Any) -> Any:
    """Recursively make a payload safe for Mongo (``date`` is not encodable)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return to_datetime(value)
    if isinstance(value, dict):
        return {k: bsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [bsonify(v) for v in value]
    return value


class TenantDocument(DBModel):
    """Every document an institution owns carries its tenant stamp.

    Present in both deployment modes — in ``dedicated`` there is simply one
    value — so the isolation logic has no mode-specific branch to get wrong.
    """

    tenant_id: PyObjectId


class SoftDeleteMixin(BaseModel):
    is_deleted: bool = False
    deleted_at: datetime | None = None


# ── Small shared value objects ────────────────────────────────────────────
class Address(AppModel):
    line1: str = ""
    line2: str = ""
    city: str = ""
    state: str = ""
    country: str = "India"
    postal_code: str = ""

    def one_line(self) -> str:
        parts = [self.line1, self.line2, self.city, self.state, self.postal_code, self.country]
        return ", ".join(p for p in parts if p)


class ContactInfo(AppModel):
    email: str = ""
    phone: str = ""
    alternate_phone: str = ""
    whatsapp: str = ""


class FileRef(AppModel):
    """A file stored in ImageKit."""

    file_id: str = ""
    name: str = ""
    url: str = ""
    thumbnail_url: str = ""
    file_path: str = ""
    size: int = 0
    mime_type: str = ""
    uploaded_at: datetime = Field(default_factory=utcnow)


class Money(AppModel):
    amount: float = 0.0
    currency: str = "INR"


class DateRange(AppModel):
    start: date
    end: date


# ── Pagination ────────────────────────────────────────────────────────────
T = TypeVar("T")


class PageMeta(AppModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_prev: bool


class Page(AppModel, Generic[T]):
    items: list[T]
    meta: PageMeta

    @classmethod
    def build(cls, items: list[T], total: int, page: int, page_size: int) -> Page[T]:
        total_pages = max(1, -(-total // page_size)) if page_size else 1
        return cls(
            items=items,
            meta=PageMeta(
                page=page,
                page_size=page_size,
                total=total,
                total_pages=total_pages,
                has_next=page < total_pages,
                has_prev=page > 1,
            ),
        )


class Msg(AppModel):
    detail: str
    success: bool = True


class IdResponse(AppModel):
    id: str
    detail: str = "Created"


def serialize_doc(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Turn a raw Mongo document into JSON-safe primitives (``_id`` -> ``id``)."""
    if doc is None:
        return None
    out: dict[str, Any] = {}
    for key, value in doc.items():
        key = "id" if key == "_id" else key
        out[key] = _serialize_value(value)
    return out


def _serialize_value(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {("id" if k == "_id" else k): _serialize_value(v) for k, v in value.items()}
    return value


__all__ = [
    "Address", "AppModel", "ContactInfo", "DBModel", "DateRange", "FileRef",
    "IdResponse", "Money", "Msg", "Page", "PageMeta", "PyObjectId",
    "SoftDeleteMixin", "TenantDocument", "bsonify", "field_serializer", "oid",
    "serialize_doc", "to_datetime", "utcnow",
]
