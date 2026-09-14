"""A resource router built from a description of the resource.

An ERP is mostly the same six endpoints over forty nouns. Writing those by hand
forty times is where inconsistencies (a missing tenant filter, a forgotten
permission, a different pagination shape) creep in. So the shape is written
once here, and each module declares what is specific to it — its collection,
its permission module, which fields are searchable, and any hooks it needs.

Anything genuinely bespoke (promoting a class, collecting a payment) is a
hand-written route on the same router, not a special case in here.
"""

# NOTE: deliberately no `from __future__ import annotations` here. The route
# signatures below are annotated with schemas built inside build_crud_router(),
# and FastAPI needs those resolved eagerly at definition time — postponed
# evaluation would leave it with unresolvable forward references.
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, create_model

from app.core.context import AuthContext, TenantContext
from app.core.deps import TenantDep, require
from app.core.exceptions import LimitExceeded, ValidationError
from app.db.repository import Repository
from app.models.base import AppModel, IdResponse, Msg, Page, serialize_doc
from app.utils.audit import diff, record

SYSTEM_FIELDS = {
    "id", "_id", "tenant_id", "created_at", "updated_at", "created_by",
    "updated_by", "is_deleted", "deleted_at",
}

Hook = Callable[..., Awaitable[Any]]


# ── Schema derivation ─────────────────────────────────────────────────────
def make_create_schema(
    model: type[BaseModel],
    name: str | None = None,
    optional: set[str] | None = None,
) -> type[BaseModel]:
    """The document model minus the fields the server owns.

    ``optional`` lists fields a before-create hook fills in (admission numbers,
    employee ids). They stay required on the document but must not be demanded
    from the client, or the hook never gets the chance to run.
    """
    optional = optional or set()
    fields: dict[str, Any] = {}
    for key, info in model.model_fields.items():
        if key in SYSTEM_FIELDS:
            continue
        if key in optional:
            annotation = info.annotation
            fields[key] = (annotation | None if annotation is not None else Any, None)
            continue
        fields[key] = (info.annotation, info)
    return create_model(  # type: ignore[call-overload]
        name or f"{model.__name__}Create", __base__=AppModel, **fields
    )


def make_update_schema(model: type[BaseModel], name: str | None = None) -> type[BaseModel]:
    """Same fields, all optional — so a PATCH never has to resend the record."""
    fields: dict[str, Any] = {}
    for key, info in model.model_fields.items():
        if key in SYSTEM_FIELDS:
            continue
        annotation = info.annotation
        optional = annotation | None if annotation is not None else Any
        fields[key] = (optional, None)
    return create_model(  # type: ignore[call-overload]
        name or f"{model.__name__}Update", __base__=AppModel, **fields
    )


# ── Resource description ──────────────────────────────────────────────────
@dataclass
class Resource:
    #: URL segment and default label source, e.g. "students".
    name: str
    #: Mongo collection.
    collection: str
    #: Permission module key — guards become "<module>:read", ":create", …
    module: str
    #: Document model; create/update schemas are derived from it.
    model: type[BaseModel]
    label: str = ""
    plural: str = ""
    tags: list[str] = field(default_factory=list)

    #: Fields a `?search=` term is matched against (case-insensitive contains).
    search_fields: list[str] = field(default_factory=list)
    #: Query params accepted as exact-match filters. ObjectId-shaped values are
    #: converted automatically, "true"/"false" become booleans.
    filters: list[str] = field(default_factory=list)
    #: Fields that may be sorted on; the first is the default.
    sortable: list[str] = field(default_factory=lambda: ["created_at"])
    default_sort_dir: str = "desc"
    #: Fields that must be unique within the institution (checked with a clear
    #: message before Mongo's index would raise a less friendly one).
    unique_fields: list[str] = field(default_factory=list)
    #: Plan limit to enforce on create, e.g. "max_students".
    limit_key: str = ""
    #: Fields a before_create hook supplies — optional on the request body.
    generated_fields: list[str] = field(default_factory=list)

    create_schema: type[BaseModel] | None = None
    update_schema: type[BaseModel] | None = None

    # Hooks — each is optional and awaited when present.
    before_create: Hook | None = None
    after_create: Hook | None = None
    before_update: Hook | None = None
    after_update: Hook | None = None
    before_delete: Hook | None = None
    after_delete: Hook | None = None
    #: Extra query constraints derived from the caller (row-level scoping, e.g.
    #: a parent only sees their own children).
    scope_hook: Callable[[AuthContext, TenantContext], Awaitable[dict]] | None = None
    #: Turn a raw document into the API shape (joins, computed extras).
    serializer: Callable[[dict], dict] | None = None

    read_only: bool = False
    soft_delete: bool = True

    def __post_init__(self) -> None:
        self.label = self.label or self.name.rstrip("s").replace("_", " ").title()
        self.plural = self.plural or self.name.replace("_", " ").title()
        self.tags = self.tags or [self.plural]
        self.create_schema = self.create_schema or make_create_schema(
            self.model, optional=set(self.generated_fields)
        )
        self.update_schema = self.update_schema or make_update_schema(self.model)


# ── Query helpers ─────────────────────────────────────────────────────────
def coerce(value: str) -> Any:
    low = value.lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"null", "none"}:
        return None
    if ObjectId.is_valid(value):
        return ObjectId(value)
    return value


def build_query(
    resource: Resource, search: str, filters: dict[str, str], extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    query: dict[str, Any] = dict(extra or {})
    for key, raw in filters.items():
        if raw in (None, ""):
            continue
        if "," in raw:
            query[key] = {"$in": [coerce(v) for v in raw.split(",") if v]}
        else:
            query[key] = coerce(raw)
    if search and resource.search_fields:
        term = search.strip()
        query["$or"] = [
            {f: {"$regex": term, "$options": "i"}} for f in resource.search_fields
        ]
    return query


async def enforce_limit(resource: Resource, tenant: TenantContext, repo: Repository) -> None:
    """Plan ceilings. A dedicated deployment has none, so this is a no-op there."""
    if not resource.limit_key or tenant.is_dedicated:
        return
    ceiling = tenant.limits.ceiling(resource.limit_key)
    if ceiling is None:
        return
    used = await repo.count()
    if used >= ceiling:
        raise LimitExceeded(
            f"Your plan covers {ceiling} {resource.plural.lower()} and you have {used}. "
            "Upgrade to add more."
        )


async def check_unique(
    resource: Resource, repo: Repository, data: dict[str, Any], exclude_id: ObjectId | None = None
) -> None:
    for key in resource.unique_fields:
        value = data.get(key)
        if value in (None, ""):
            continue
        query: dict[str, Any] = {key: value}
        if exclude_id:
            query["_id"] = {"$ne": exclude_id}
        if await repo.exists(query):
            pretty = key.replace("_", " ")
            raise ValidationError(
                f"A {resource.label.lower()} with that {pretty} already exists"
            )


# ── Router factory ────────────────────────────────────────────────────────
def build_crud_router(resource: Resource) -> APIRouter:
    router = APIRouter(prefix=f"/{resource.name}", tags=resource.tags)
    module = resource.module
    CreateSchema = resource.create_schema
    UpdateSchema = resource.update_schema

    def repo_of(tenant: TenantContext, auth: AuthContext) -> Repository:
        return Repository(
            resource.collection, tenant.id, actor_id=auth.user_id,
            soft_delete=resource.soft_delete,
        )

    def present(doc: dict) -> dict:
        out = serialize_doc(doc) or {}
        return resource.serializer(out) if resource.serializer else out

    Reader = Annotated[AuthContext, Depends(require(f"{module}:read"))]
    Creator = Annotated[AuthContext, Depends(require(f"{module}:create"))]
    Updater = Annotated[AuthContext, Depends(require(f"{module}:update"))]
    Deleter = Annotated[AuthContext, Depends(require(f"{module}:delete"))]

    @router.get("", response_model=Page[dict], summary=f"List {resource.plural.lower()}")
    async def list_items(
        request: Request,
        auth: Reader,
        tenant: TenantDep,
        page: int = 1,
        page_size: Annotated[int, Query(le=200)] = 25,
        search: str = "",
        sort_by: str | None = None,
        sort_dir: str | None = None,
        include_deleted: bool = False,
    ):
        repo = repo_of(tenant, auth)
        filters = {
            key: value
            for key, value in request.query_params.items()
            if key in resource.filters
        }
        extra = await resource.scope_hook(auth, tenant) if resource.scope_hook else {}
        query = build_query(resource, search, filters, extra)
        if include_deleted and auth.can(f"{module}:delete"):
            query["is_deleted"] = {"$in": [True, False]}
        chosen_sort = sort_by if sort_by in resource.sortable else resource.sortable[0]
        result = await repo.paginate(
            query,
            page=page,
            page_size=page_size,
            sort_by=chosen_sort,
            sort_dir=(sort_dir or resource.default_sort_dir),  # type: ignore[arg-type]
        )
        if resource.serializer:
            result.items = [resource.serializer(item) for item in result.items]
        return result

    @router.get("/{item_id}", summary=f"Get one {resource.label.lower()}")
    async def get_item(item_id: str, auth: Reader, tenant: TenantDep):
        repo = repo_of(tenant, auth)
        doc = await repo.get_or_404(item_id, label=resource.label)
        return present(doc)

    if not resource.read_only:

        @router.post("", response_model=IdResponse, status_code=201,
                     summary=f"Create a {resource.label.lower()}")
        async def create_item(
            auth: Creator,
            tenant: TenantDep,
            request: Request,
            payload: Annotated[CreateSchema, Body()],  # type: ignore[valid-type]
        ):
            repo = repo_of(tenant, auth)
            await enforce_limit(resource, tenant, repo)
            data = payload.model_dump(exclude_none=True)  # type: ignore[attr-defined]
            if resource.before_create:
                data = await resource.before_create(data, auth, tenant, repo) or data
            await check_unique(resource, repo, data)
            doc = await repo.create(data)
            if resource.after_create:
                await resource.after_create(doc, auth, tenant, repo)
            await record(auth, f"{resource.name}.create", entity_type=resource.name,
                         entity_id=doc["_id"], entity_label=_label_of(doc), request=request)
            return IdResponse(id=str(doc["_id"]), detail=f"{resource.label} created")

        @router.patch("/{item_id}", summary=f"Update a {resource.label.lower()}")
        async def update_item(
            item_id: str,
            auth: Updater,
            tenant: TenantDep,
            request: Request,
            payload: Annotated[UpdateSchema, Body()],  # type: ignore[valid-type]
        ):
            repo = repo_of(tenant, auth)
            before = await repo.get_or_404(item_id, label=resource.label)
            data = payload.model_dump(exclude_unset=True, exclude_none=True)  # type: ignore[attr-defined]
            if not data:
                raise ValidationError("Nothing to update")
            if resource.before_update:
                data = await resource.before_update(data, before, auth, tenant, repo) or data
            await check_unique(resource, repo, data, exclude_id=before["_id"])
            after = await repo.update(item_id, data, label=resource.label)
            if resource.after_update:
                await resource.after_update(after, before, auth, tenant, repo)
            await record(auth, f"{resource.name}.update", entity_type=resource.name,
                         entity_id=after["_id"], entity_label=_label_of(after),
                         changes=diff(before, after), request=request)
            return present(after)

        @router.delete("/{item_id}", response_model=Msg,
                       summary=f"Delete a {resource.label.lower()}")
        async def delete_item(item_id: str, auth: Deleter, tenant: TenantDep, request: Request):
            repo = repo_of(tenant, auth)
            doc = await repo.get_or_404(item_id, label=resource.label)
            if resource.before_delete:
                await resource.before_delete(doc, auth, tenant, repo)
            await repo.delete(item_id, label=resource.label)
            if resource.after_delete:
                await resource.after_delete(doc, auth, tenant, repo)
            await record(auth, f"{resource.name}.delete", entity_type=resource.name,
                         entity_id=doc["_id"], entity_label=_label_of(doc), request=request)
            return Msg(detail=f"{resource.label} deleted")

        @router.post("/{item_id}/restore", summary=f"Restore a deleted {resource.label.lower()}")
        async def restore_item(item_id: str, auth: Deleter, tenant: TenantDep, request: Request):
            repo = repo_of(tenant, auth)
            doc = await repo.restore(item_id)
            if doc is None:
                from app.core.exceptions import NotFound

                raise NotFound(f"{resource.label} not found")
            await record(auth, f"{resource.name}.restore", entity_type=resource.name,
                         entity_id=doc["_id"], request=request)
            return present(doc)

        @router.post("/bulk-delete", response_model=Msg,
                     summary=f"Delete several {resource.plural.lower()}")
        async def bulk_delete(
            auth: Deleter, tenant: TenantDep, request: Request,
            ids: Annotated[list[str], Body(embed=True)],
        ):
            repo = repo_of(tenant, auth)
            deleted = 0
            for item_id in ids[:500]:
                try:
                    await repo.delete(item_id, label=resource.label)
                    deleted += 1
                except Exception:  # skip ones that are already gone
                    continue
            await record(auth, f"{resource.name}.bulk_delete", entity_type=resource.name,
                         changes={"count": deleted}, request=request)
            return Msg(detail=f"{deleted} {resource.plural.lower()} deleted")

    return router


def _label_of(doc: dict[str, Any]) -> str:
    for key in ("name", "full_name", "title", "number", "code", "email"):
        value = doc.get(key)
        if value:
            return str(value)
    first, last = doc.get("first_name", ""), doc.get("last_name", "")
    return f"{first} {last}".strip() or str(doc.get("_id", ""))
