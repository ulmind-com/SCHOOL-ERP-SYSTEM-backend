"""Company-side operations: institutions, plans, subscriptions, metrics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from bson import ObjectId

from app.core.exceptions import Conflict, NotFound, ValidationError
from app.core.security import create_token
from app.core.tenancy import invalidate_tenant_cache
from app.db.mongo import C, collection
from app.models.base import serialize_doc, utcnow
from app.models.catalog import PLANS_BY_KEY
from app.models.tenant import RESERVED_SLUGS

MONTHS = 12


def _dt(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


# ── Institutions ──────────────────────────────────────────────────────────
async def list_tenants(
    *,
    page: int = 1,
    page_size: int = 25,
    search: str = "",
    status: str = "",
    deployment: str = "",
    plan_key: str = "",
    sort_by: str = "created_at",
    sort_dir: str = "desc",
) -> dict[str, Any]:
    query: dict[str, Any] = {"is_deleted": {"$ne": True}}
    if status:
        query["status"] = status
    if deployment:
        query["deployment"] = deployment
    if plan_key:
        query["plan_key"] = plan_key
    if search:
        term = search.strip()
        query["$or"] = [
            {"name": {"$regex": term, "$options": "i"}},
            {"slug": {"$regex": term, "$options": "i"}},
            {"contact.email": {"$regex": term, "$options": "i"}},
        ]

    tenants = collection(C.TENANTS)
    total = await tenants.count_documents(query)
    direction = 1 if sort_dir == "asc" else -1
    docs = await (
        tenants.find(query)
        .sort([(sort_by, direction)])
        .skip((max(1, page) - 1) * page_size)
        .limit(page_size)
        .to_list(length=page_size)
    )

    # Attach live usage counts in one round-trip per collection.
    ids = [d["_id"] for d in docs]
    students = await _counts_by_tenant(C.STUDENTS, ids, {"status": "active"})
    staff = await _counts_by_tenant(C.STAFF, ids, {"status": "active"})
    users = await _counts_by_tenant(C.USERS, ids, {"is_active": True})

    items = []
    for doc in docs:
        row = serialize_doc(doc) or {}
        row.pop("license_key", None)  # never list secrets in bulk
        row["usage"] = {
            "students": students.get(doc["_id"], 0),
            "staff": staff.get(doc["_id"], 0),
            "users": users.get(doc["_id"], 0),
        }
        items.append(row)

    total_pages = max(1, -(-total // page_size))
    return {
        "items": items,
        "meta": {
            "page": page, "page_size": page_size, "total": total,
            "total_pages": total_pages, "has_next": page < total_pages, "has_prev": page > 1,
        },
    }


async def _counts_by_tenant(
    name: str, tenant_ids: list[ObjectId], extra: dict[str, Any] | None = None
) -> dict[ObjectId, int]:
    if not tenant_ids:
        return {}
    match = {"tenant_id": {"$in": tenant_ids}, "is_deleted": {"$ne": True}, **(extra or {})}
    rows = await collection(name).aggregate(
        [{"$match": match}, {"$group": {"_id": "$tenant_id", "n": {"$sum": 1}}}]
    ).to_list(length=None)
    return {r["_id"]: r["n"] for r in rows}


async def get_tenant_detail(tenant_id: str | ObjectId) -> dict[str, Any]:
    _id = ObjectId(tenant_id) if isinstance(tenant_id, str) else tenant_id
    doc = await collection(C.TENANTS).find_one({"_id": _id})
    if doc is None:
        raise NotFound("Institution not found")

    detail = serialize_doc(doc) or {}
    base = {"tenant_id": _id, "is_deleted": {"$ne": True}}
    detail["usage"] = {
        "students": await collection(C.STUDENTS).count_documents({**base, "status": "active"}),
        "staff": await collection(C.STAFF).count_documents({**base, "status": "active"}),
        "users": await collection(C.USERS).count_documents({**base, "is_active": True}),
        "classes": await collection(C.CLASSES).count_documents(base),
        "storage_mb": round(float(doc.get("storage_used_mb", 0)), 2),
    }
    subscription = await collection(C.SUBSCRIPTIONS).find_one(
        {"tenant_id": _id}, sort=[("created_at", -1)]
    )
    detail["subscription"] = serialize_doc(subscription)
    owner = await collection(C.USERS).find_one(
        {"tenant_id": _id, "is_deleted": {"$ne": True}}, sort=[("created_at", 1)]
    )
    detail["owner"] = (
        {"id": str(owner["_id"]), "name": owner.get("full_name"), "email": owner.get("email"),
         "last_login_at": owner.get("last_login_at")}
        if owner else None
    )
    invoices = await collection(C.PLATFORM_INVOICES).find(
        {"tenant_id": _id}
    ).sort([("created_at", -1)]).limit(12).to_list(length=12)
    detail["invoices"] = [serialize_doc(i) for i in invoices]
    return detail


async def update_tenant(tenant_id: ObjectId, data: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in data.items() if v is not None}
    if not payload:
        raise ValidationError("Nothing to update")
    payload["updated_at"] = utcnow()
    doc = await collection(C.TENANTS).find_one_and_update(
        {"_id": tenant_id}, {"$set": payload}, return_document=True
    )
    if doc is None:
        raise NotFound("Institution not found")
    invalidate_tenant_cache()
    return serialize_doc(doc)  # type: ignore[return-value]


async def set_tenant_status(tenant_id: ObjectId, status: str, reason: str = "") -> dict[str, Any]:
    if status not in {"active", "trial", "suspended", "cancelled"}:
        raise ValidationError(f"'{status}' is not a valid status")
    update: dict[str, Any] = {"status": status, "updated_at": utcnow()}
    if reason:
        update["notes"] = reason
    if status == "cancelled":
        update["subscription_status"] = "cancelled"
    elif status == "active":
        update.setdefault("subscription_status", "active")
    doc = await collection(C.TENANTS).find_one_and_update(
        {"_id": tenant_id}, {"$set": update}, return_document=True
    )
    if doc is None:
        raise NotFound("Institution not found")
    invalidate_tenant_cache()
    return serialize_doc(doc)  # type: ignore[return-value]


async def check_slug(slug: str) -> dict[str, Any]:
    slug = slug.strip().lower()
    if slug in RESERVED_SLUGS:
        return {"slug": slug, "available": False, "reason": "This address is reserved"}
    if len(slug) < 3:
        return {"slug": slug, "available": False, "reason": "Use at least 3 characters"}
    if not all(c.isalnum() or c == "-" for c in slug):
        return {"slug": slug, "available": False,
                "reason": "Only lowercase letters, numbers and hyphens"}
    taken = await collection(C.TENANTS).find_one({"slug": slug})
    return {
        "slug": slug,
        "available": taken is None,
        "reason": "Already taken" if taken else "",
    }


# ── Subscriptions ─────────────────────────────────────────────────────────
async def change_subscription(
    tenant_id: ObjectId,
    plan_key: str,
    *,
    billing_cycle: str = "yearly",
    amount: float | None = None,
    valid_till: date | None = None,
    payment_reference: str = "",
    note: str = "",
    actor: str = "",
) -> dict[str, Any]:
    tenant = await collection(C.TENANTS).find_one({"_id": tenant_id})
    if tenant is None:
        raise NotFound("Institution not found")
    plan = await collection(C.PLANS).find_one({"key": plan_key}) or PLANS_BY_KEY.get(plan_key)
    if plan is None:
        raise NotFound(f"No plan called '{plan_key}'")

    lifetime = billing_cycle == "lifetime" or plan_key == "lifetime"
    if valid_till is None and not lifetime:
        days = 30 if billing_cycle == "monthly" else 365
        valid_till = date.today() + timedelta(days=days)

    price = amount
    if price is None:
        price = float(
            plan.get("price_lifetime" if lifetime
                     else ("price_monthly" if billing_cycle == "monthly" else "price_yearly"), 0)
            or 0
        )

    await collection(C.TENANTS).update_one(
        {"_id": tenant_id},
        {
            "$set": {
                "plan_key": plan_key,
                "status": "active",
                "subscription_status": "lifetime" if lifetime else "active",
                "subscription_valid_till": _dt(valid_till),
                "limits": plan.get("limits", {}),
                "enabled_modules": list(plan.get("included_modules", [])),
                "updated_at": utcnow(),
            }
        },
    )
    doc = await collection(C.SUBSCRIPTIONS).find_one_and_update(
        {"tenant_id": tenant_id},
        {
            "$set": {
                "plan_key": plan_key,
                "status": "lifetime" if lifetime else "active",
                "billing_cycle": billing_cycle,
                "amount": price,
                "current_period_start": _dt(date.today()),
                "current_period_end": _dt(valid_till),
                "payment_reference": payment_reference,
                "updated_at": utcnow(),
            },
            "$push": {
                "history": {
                    "at": utcnow(), "event": "plan_changed", "plan_key": plan_key,
                    "amount": price, "by": actor, "note": note,
                }
            },
            "$setOnInsert": {"tenant_id": tenant_id, "created_at": utcnow(), "is_deleted": False},
        },
        upsert=True,
        return_document=True,
    )
    invalidate_tenant_cache()
    return serialize_doc(doc)  # type: ignore[return-value]


async def list_subscriptions(*, status: str = "", expiring_days: int | None = None) -> list[dict]:
    query: dict[str, Any] = {"is_deleted": {"$ne": True}}
    if status:
        query["status"] = status
    if expiring_days is not None:
        query["current_period_end"] = {
            "$gte": _dt(date.today()),
            "$lte": _dt(date.today() + timedelta(days=expiring_days)),
        }
    rows = await collection(C.SUBSCRIPTIONS).find(query).sort(
        [("current_period_end", 1)]
    ).to_list(length=500)
    tenant_ids = [r["tenant_id"] for r in rows]
    tenants = {
        t["_id"]: t
        for t in await collection(C.TENANTS).find({"_id": {"$in": tenant_ids}}).to_list(length=None)
    }
    out = []
    for row in rows:
        item = serialize_doc(row) or {}
        tenant = tenants.get(row["tenant_id"], {})
        item["institution"] = {
            "id": str(row["tenant_id"]),
            "name": tenant.get("name", ""),
            "slug": tenant.get("slug", ""),
            "status": tenant.get("status", ""),
            "deployment": tenant.get("deployment", "saas"),
        }
        out.append(item)
    return out


# ── Plans ─────────────────────────────────────────────────────────────────
async def list_plans(*, public_only: bool = False) -> list[dict]:
    query: dict[str, Any] = {"is_deleted": {"$ne": True}, "is_active": True}
    if public_only:
        query["is_public"] = True
    rows = await collection(C.PLANS).find(query).sort([("sort_order", 1)]).to_list(length=None)
    return [serialize_doc(r) for r in rows]  # type: ignore[misc]


async def upsert_plan(data: dict[str, Any]) -> dict[str, Any]:
    key = data["key"].strip().lower()
    doc = await collection(C.PLANS).find_one_and_update(
        {"key": key},
        {"$set": {**data, "key": key, "updated_at": utcnow()},
         "$setOnInsert": {"created_at": utcnow(), "is_deleted": False}},
        upsert=True,
        return_document=True,
    )
    return serialize_doc(doc)  # type: ignore[return-value]


async def delete_plan(key: str) -> None:
    in_use = await collection(C.TENANTS).count_documents(
        {"plan_key": key, "is_deleted": {"$ne": True}}
    )
    if in_use:
        raise Conflict(f"{in_use} institution(s) are on this plan. Move them first.")
    await collection(C.PLANS).update_one(
        {"key": key}, {"$set": {"is_deleted": True, "updated_at": utcnow()}}
    )


# ── Metrics ───────────────────────────────────────────────────────────────
async def platform_metrics() -> dict[str, Any]:
    tenants = collection(C.TENANTS)
    live = {"is_deleted": {"$ne": True}}
    month_start = datetime(date.today().year, date.today().month, 1, tzinfo=UTC)

    by_status = {
        r["_id"]: r["n"]
        for r in await tenants.aggregate(
            [{"$match": live}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]
        ).to_list(length=None)
    }
    by_plan = await tenants.aggregate(
        [{"$match": live}, {"$group": {"_id": "$plan_key", "count": {"$sum": 1}}},
         {"$sort": {"count": -1}}]
    ).to_list(length=None)
    by_type = await tenants.aggregate(
        [{"$match": live}, {"$group": {"_id": "$institution_type", "count": {"$sum": 1}}},
         {"$sort": {"count": -1}}]
    ).to_list(length=None)

    revenue = await collection(C.SUBSCRIPTIONS).aggregate(
        [
            {"$match": {"status": {"$in": ["active"]}, "is_deleted": {"$ne": True}}},
            {"$group": {
                "_id": "$billing_cycle",
                "total": {"$sum": "$amount"},
                "count": {"$sum": 1},
            }},
        ]
    ).to_list(length=None)
    mrr = 0.0
    for row in revenue:
        if row["_id"] == "monthly":
            mrr += float(row["total"])
        elif row["_id"] == "yearly":
            mrr += float(row["total"]) / MONTHS

    recent = await tenants.find(live).sort([("created_at", -1)]).limit(8).to_list(length=8)

    return {
        "institutions_total": sum(by_status.values()),
        "institutions_active": by_status.get("active", 0),
        "institutions_trial": by_status.get("trial", 0),
        "institutions_suspended": by_status.get("suspended", 0),
        "institutions_dedicated": await tenants.count_documents(
            {**live, "deployment": "dedicated"}
        ),
        "students_total": await collection(C.STUDENTS).count_documents(
            {"is_deleted": {"$ne": True}, "status": "active"}
        ),
        "staff_total": await collection(C.STAFF).count_documents(
            {"is_deleted": {"$ne": True}, "status": "active"}
        ),
        "mrr": round(mrr, 2),
        "arr": round(mrr * MONTHS, 2),
        "expiring_in_30_days": await tenants.count_documents(
            {**live, "subscription_valid_till": {
                "$gte": _dt(date.today()),
                "$lte": _dt(date.today() + timedelta(days=30)),
            }}
        ),
        "signups_this_month": await tenants.count_documents(
            {**live, "created_at": {"$gte": month_start}}
        ),
        "by_plan": [{"plan_key": r["_id"] or "none", "count": r["count"]} for r in by_plan],
        "by_type": [{"type": r["_id"] or "school", "count": r["count"]} for r in by_type],
        "recent_signups": [
            {
                "id": str(t["_id"]), "name": t.get("name"), "slug": t.get("slug"),
                "plan_key": t.get("plan_key"), "status": t.get("status"),
                "deployment": t.get("deployment"), "created_at": t.get("created_at"),
            }
            for t in recent
        ],
    }


async def impersonation_token(tenant_id: ObjectId, actor_email: str) -> dict[str, Any]:
    """Short-lived access to an institution for support.

    Issued against the institution's owner account, flagged ``imp`` so the
    session shows up as impersonated and lands in that institution's own audit
    trail rather than silently.
    """
    tenant = await collection(C.TENANTS).find_one({"_id": tenant_id})
    if tenant is None:
        raise NotFound("Institution not found")
    owner = await collection(C.USERS).find_one(
        {"tenant_id": tenant_id, "is_active": True, "is_deleted": {"$ne": True}},
        sort=[("created_at", 1)],
    )
    if owner is None:
        raise NotFound("This institution has no active user to act as")
    token = create_token(
        str(owner["_id"]), "access", tenant_id=str(tenant_id),
        expires_delta=timedelta(minutes=30), extra={"imp": actor_email},
    )
    return {
        "access_token": token,
        "expires_in": 1800,
        "institution": {"id": str(tenant_id), "name": tenant.get("name"),
                        "slug": tenant.get("slug")},
        "acting_as": {"id": str(owner["_id"]), "email": owner.get("email"),
                      "name": owner.get("full_name")},
    }
