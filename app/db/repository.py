"""Tenant-scoped data access.

Every read and write funnels through here, and the tenant filter is added by
the repository rather than by callers. A route that forgets to pass
``tenant_id`` gets no data at all instead of somebody else's — the failure mode
points the safe way.
"""

from __future__ import annotations

from typing import Any, Literal

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.exceptions import Conflict, NotFound
from app.db.mongo import collection
from app.models.base import Page, bsonify, oid, serialize_doc, utcnow

SortDir = Literal["asc", "desc"]


class Repository:
    """A collection bound to one institution."""

    def __init__(
        self,
        name: str,
        tenant_id: ObjectId | str | None = None,
        *,
        actor_id: ObjectId | None = None,
        tenant_scoped: bool = True,
        soft_delete: bool = True,
    ):
        self.name = name
        self.tenant_id = oid(tenant_id) if tenant_id else None
        self.actor_id = actor_id
        self.tenant_scoped = tenant_scoped
        self.soft_delete = soft_delete
        if tenant_scoped and self.tenant_id is None:
            raise ValueError(f"Repository('{name}') is tenant-scoped but got no tenant_id")

    @property
    def col(self) -> AsyncIOMotorCollection:
        return collection(self.name)

    # ── Filter construction ───────────────────────────────────────────────
    def scope(self, query: dict[str, Any] | None = None, *, include_deleted: bool = False) -> dict:
        q: dict[str, Any] = dict(query or {})
        if self.tenant_scoped:
            q["tenant_id"] = self.tenant_id
        if self.soft_delete and not include_deleted and "is_deleted" not in q:
            q["is_deleted"] = {"$ne": True}
        return q

    @staticmethod
    def _sort(sort_by: str | None, sort_dir: SortDir) -> list[tuple[str, int]]:
        direction = ASCENDING if sort_dir == "asc" else DESCENDING
        return [(sort_by or "created_at", direction)]

    # ── Reads ─────────────────────────────────────────────────────────────
    async def get(self, doc_id: str | ObjectId, *, include_deleted: bool = False) -> dict | None:
        _id = oid(doc_id)
        if _id is None:
            return None
        return await self.col.find_one(self.scope({"_id": _id}, include_deleted=include_deleted))

    async def get_or_404(self, doc_id: str | ObjectId, *, label: str | None = None) -> dict:
        doc = await self.get(doc_id)
        if doc is None:
            raise NotFound(f"{label or self.name.rstrip('s').replace('_', ' ').title()} not found")
        return doc

    async def find_one(self, query: dict[str, Any], *, include_deleted: bool = False) -> dict | None:
        return await self.col.find_one(self.scope(query, include_deleted=include_deleted))

    async def exists(self, query: dict[str, Any]) -> bool:
        return await self.col.count_documents(self.scope(query), limit=1) > 0

    async def count(self, query: dict[str, Any] | None = None) -> int:
        return await self.col.count_documents(self.scope(query))

    async def list(
        self,
        query: dict[str, Any] | None = None,
        *,
        sort_by: str | None = None,
        sort_dir: SortDir = "desc",
        limit: int = 0,
        skip: int = 0,
        projection: dict[str, Any] | None = None,
    ) -> list[dict]:
        cursor = self.col.find(self.scope(query), projection).sort(self._sort(sort_by, sort_dir))
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        return await cursor.to_list(length=limit or None)

    async def paginate(
        self,
        query: dict[str, Any] | None = None,
        *,
        page: int = 1,
        page_size: int = 25,
        sort_by: str | None = None,
        sort_dir: SortDir = "desc",
        projection: dict[str, Any] | None = None,
        serialize: bool = True,
    ) -> Page[dict]:
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        scoped = self.scope(query)
        total = await self.col.count_documents(scoped)
        cursor = (
            self.col.find(scoped, projection)
            .sort(self._sort(sort_by, sort_dir))
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        docs = await cursor.to_list(length=page_size)
        items = [serialize_doc(d) for d in docs] if serialize else docs
        return Page[dict].build(items, total, page, page_size)

    async def aggregate(self, pipeline: list[dict], *, scoped: bool = True) -> list[dict]:
        stages = ([{"$match": self.scope()}] if scoped else []) + pipeline
        return await self.col.aggregate(stages).to_list(length=None)

    async def distinct(self, key: str, query: dict[str, Any] | None = None) -> list:
        return await self.col.distinct(key, self.scope(query))

    # ── Writes ────────────────────────────────────────────────────────────
    def _stamp_new(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        doc = bsonify(dict(data))
        doc.pop("_id", None)
        doc.pop("id", None)
        if self.tenant_scoped:
            doc["tenant_id"] = self.tenant_id
        doc.setdefault("created_at", now)
        doc["updated_at"] = now
        doc.setdefault("is_deleted", False)
        if self.actor_id:
            doc.setdefault("created_by", self.actor_id)
            doc["updated_by"] = self.actor_id
        return doc

    async def create(self, data: dict[str, Any], *, conflict_message: str | None = None) -> dict:
        doc = self._stamp_new(data)
        try:
            result = await self.col.insert_one(doc)
        except DuplicateKeyError as exc:
            raise Conflict(conflict_message or _duplicate_message(exc)) from exc
        doc["_id"] = result.inserted_id
        return doc

    async def create_many(self, rows: list[dict[str, Any]]) -> list[ObjectId]:
        if not rows:
            return []
        docs = [self._stamp_new(r) for r in rows]
        result = await self.col.insert_many(docs, ordered=False)
        return list(result.inserted_ids)

    async def update(
        self,
        doc_id: str | ObjectId,
        data: dict[str, Any],
        *,
        unset: list[str] | None = None,
        label: str | None = None,
    ) -> dict:
        _id = oid(doc_id)
        if _id is None:
            raise NotFound(f"{label or self.name} not found")
        payload = bsonify(
            {k: v for k, v in data.items() if k not in {"_id", "id", "tenant_id", "created_at"}}
        )
        update: dict[str, Any] = {"$set": {**payload, "updated_at": utcnow()}}
        if self.actor_id:
            update["$set"]["updated_by"] = self.actor_id
        if unset:
            update["$unset"] = dict.fromkeys(unset, "")
        try:
            doc = await self.col.find_one_and_update(
                self.scope({"_id": _id}), update, return_document=ReturnDocument.AFTER
            )
        except DuplicateKeyError as exc:
            raise Conflict(_duplicate_message(exc)) from exc
        if doc is None:
            raise NotFound(f"{label or self.name} not found")
        return doc

    async def update_many(self, query: dict[str, Any], data: dict[str, Any]) -> int:
        result = await self.col.update_many(
            self.scope(query), {"$set": {**data, "updated_at": utcnow()}}
        )
        return result.modified_count

    async def upsert(self, query: dict[str, Any], data: dict[str, Any]) -> dict:
        now = utcnow()
        on_insert: dict[str, Any] = {"created_at": now, "is_deleted": False}
        if self.tenant_scoped:
            on_insert["tenant_id"] = self.tenant_id
        if self.actor_id:
            on_insert["created_by"] = self.actor_id
        payload = bsonify({k: v for k, v in data.items() if k not in on_insert and k != "_id"})
        return await self.col.find_one_and_update(
            self.scope(query),
            {"$set": {**payload, "updated_at": now}, "$setOnInsert": on_insert},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def push(self, doc_id: str | ObjectId, field: str, value: Any) -> dict | None:
        return await self.col.find_one_and_update(
            self.scope({"_id": oid(doc_id)}),
            {"$push": {field: value}, "$set": {"updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )

    async def pull(self, doc_id: str | ObjectId, field: str, match: Any) -> dict | None:
        return await self.col.find_one_and_update(
            self.scope({"_id": oid(doc_id)}),
            {"$pull": {field: match}, "$set": {"updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )

    async def increment(self, doc_id: str | ObjectId, field: str, by: int | float = 1) -> dict | None:
        return await self.col.find_one_and_update(
            self.scope({"_id": oid(doc_id)}),
            {"$inc": {field: by}, "$set": {"updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )

    # ── Deletes ───────────────────────────────────────────────────────────
    async def delete(self, doc_id: str | ObjectId, *, label: str | None = None) -> None:
        """Soft delete by default so nothing an institution owns vanishes."""
        _id = oid(doc_id)
        if _id is None:
            raise NotFound(f"{label or self.name} not found")
        if self.soft_delete:
            result = await self.col.update_one(
                self.scope({"_id": _id}),
                {"$set": {"is_deleted": True, "deleted_at": utcnow(), "updated_at": utcnow()}},
            )
            if result.matched_count == 0:
                raise NotFound(f"{label or self.name} not found")
            return
        result = await self.col.delete_one(self.scope({"_id": _id}))
        if result.deleted_count == 0:
            raise NotFound(f"{label or self.name} not found")

    async def restore(self, doc_id: str | ObjectId) -> dict | None:
        return await self.col.find_one_and_update(
            self.scope({"_id": oid(doc_id)}, include_deleted=True),
            {"$set": {"is_deleted": False, "updated_at": utcnow()}, "$unset": {"deleted_at": ""}},
            return_document=ReturnDocument.AFTER,
        )

    async def hard_delete_many(self, query: dict[str, Any]) -> int:
        result = await self.col.delete_many(self.scope(query, include_deleted=True))
        return result.deleted_count


def _duplicate_message(exc: DuplicateKeyError) -> str:
    key = (exc.details or {}).get("keyValue") or {}
    readable = ", ".join(f"{k.replace('_', ' ')} '{v}'" for k, v in key.items() if k != "tenant_id")
    return f"{readable} already exists" if readable else "That value already exists"


async def next_sequence(tenant_id: ObjectId, key: str, *, start: int = 1) -> int:
    """Atomic per-institution counter — admission numbers, receipt numbers…"""
    doc = await collection("counters").find_one_and_update(
        {"tenant_id": tenant_id, "key": key},
        {"$inc": {"value": 1}, "$setOnInsert": {"tenant_id": tenant_id, "key": key}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    value = doc.get("value", 1)
    return value + start - 1 if start != 1 else value
