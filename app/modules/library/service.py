"""Library circulation.

Copy counts are the thing to get right: `available_copies` is decremented on
issue and restored on return, and both happen in the same call as the loan
record so a crash cannot leave a book issued but still shown as available.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.core.exceptions import Conflict, NotFound
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models.base import serialize_doc, to_datetime, utcnow
from app.models.finance import money

DEFAULT_LOAN_DAYS = 14
DEFAULT_FINE_PER_DAY = 2.0
MAX_RENEWALS = 2


def _rules(tenant: TenantContext) -> dict[str, Any]:
    rules = (tenant.settings or {}).get("library", {})
    return {
        "loan_days": int(rules.get("loan_days", DEFAULT_LOAN_DAYS)),
        "fine_per_day": float(rules.get("fine_per_day", DEFAULT_FINE_PER_DAY)),
        "max_books_student": int(rules.get("max_books_student", 2)),
        "max_books_staff": int(rules.get("max_books_staff", 5)),
        "max_renewals": int(rules.get("max_renewals", MAX_RENEWALS)),
    }


async def _borrower(
    tenant: TenantContext, borrower_type: str, borrower_id: ObjectId
) -> dict[str, Any]:
    target = C.STAFF if borrower_type == "staff" else C.STUDENTS
    person = await collection(target).find_one(
        {"_id": borrower_id, "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if person is None:
        raise NotFound("Borrower not found")
    return person


def _name(person: dict[str, Any]) -> str:
    return " ".join(filter(None, [person.get("first_name"), person.get("last_name")])) or \
        person.get("full_name", "")


async def issue(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    item_id: str,
    borrower_type: str,
    borrower_id: str,
    due_date: date | None = None,
) -> dict[str, Any]:
    rules = _rules(tenant)
    items = Repository(C.LIBRARY_ITEMS, tenant.id, actor_id=auth.user_id)
    loans = Repository(C.LIBRARY_LOANS, tenant.id, actor_id=auth.user_id)

    item = await items.get_or_404(item_id, label="Library item")
    if int(item.get("available_copies") or 0) <= 0:
        raise Conflict(f"All copies of '{item.get('title', '')}' are out")

    borrower_oid = ObjectId(borrower_id)
    person = await _borrower(tenant, borrower_type, borrower_oid)

    ceiling = rules["max_books_staff"] if borrower_type == "staff" else rules["max_books_student"]
    on_loan = await loans.count({"borrower_id": borrower_oid, "status": {"$in": ["issued", "overdue"]}})
    if on_loan >= ceiling:
        raise Conflict(
            f"{_name(person)} already has {on_loan} item(s) out, and the limit is {ceiling}"
        )

    if await loans.exists({"item_id": item["_id"], "borrower_id": borrower_oid,
                           "status": {"$in": ["issued", "overdue"]}}):
        raise Conflict("That borrower already has this item")

    due = due_date or (date.today() + timedelta(days=rules["loan_days"]))
    loan = await loans.create({
        "item_id": item["_id"],
        "item_title": item.get("title", ""),
        "borrower_type": borrower_type,
        "borrower_id": borrower_oid,
        "borrower_name": _name(person),
        "issued_on": utcnow(),
        "due_date": to_datetime(due),
        "status": "issued",
        "renewed_count": 0,
        "fine_amount": 0.0,
        "fine_paid": False,
        "issued_by": auth.user_id,
    })

    remaining = int(item.get("available_copies") or 0) - 1
    await items.update(item_id, {
        "available_copies": remaining,
        "status": "issued" if remaining <= 0 else item.get("status", "available"),
    })

    return {
        **(serialize_doc(loan) or {}),
        "detail": f"'{item.get('title', '')}' issued to {_name(person)}, due {due:%d %b %Y}",
    }


def fine_for(due_date: Any, returned_on: date, fine_per_day: float) -> tuple[int, float]:
    if due_date is None:
        return 0, 0.0
    due = due_date.date() if hasattr(due_date, "date") else due_date
    late_days = max((returned_on - due).days, 0)
    return late_days, money(late_days * fine_per_day)


async def return_item(
    tenant: TenantContext,
    auth: AuthContext,
    loan_id: str,
    *,
    condition: str = "good",
    waive_fine: bool = False,
) -> dict[str, Any]:
    rules = _rules(tenant)
    loans = Repository(C.LIBRARY_LOANS, tenant.id, actor_id=auth.user_id)
    items = Repository(C.LIBRARY_ITEMS, tenant.id, actor_id=auth.user_id)

    loan = await loans.get_or_404(loan_id, label="Loan")
    if loan.get("status") == "returned":
        raise Conflict("This item has already been returned")

    today = date.today()
    late_days, fine = fine_for(loan.get("due_date"), today, rules["fine_per_day"])
    if waive_fine:
        fine = 0.0

    updated = await loans.update(loan_id, {
        "returned_on": utcnow(),
        "status": "lost" if condition == "lost" else "returned",
        "fine_amount": fine,
        "fine_waived": waive_fine,
        "return_condition": condition,
        "received_by": auth.user_id,
    })

    # A lost copy does not come back to the shelf.
    item = await items.get(loan["item_id"])
    if item and condition != "lost":
        await items.update(str(item["_id"]), {
            "available_copies": int(item.get("available_copies") or 0) + 1,
            "status": "damaged" if condition == "damaged" else "available",
        })
    elif item and condition == "lost":
        await items.update(str(item["_id"]), {
            "total_copies": max(int(item.get("total_copies") or 1) - 1, 0),
        })

    return {
        **(serialize_doc(updated) or {}),
        "late_days": late_days,
        "fine": fine,
        "detail": (
            f"Returned{f' — {late_days} day(s) late, fine {fine}' if fine else ''}"
            if condition != "lost" else "Marked lost and removed from stock"
        ),
    }


async def renew(tenant: TenantContext, auth: AuthContext, loan_id: str) -> dict[str, Any]:
    rules = _rules(tenant)
    loans = Repository(C.LIBRARY_LOANS, tenant.id, actor_id=auth.user_id)
    loan = await loans.get_or_404(loan_id, label="Loan")

    if loan.get("status") not in {"issued", "overdue"}:
        raise Conflict("Only an item that is out can be renewed")
    if int(loan.get("renewed_count") or 0) >= rules["max_renewals"]:
        raise Conflict(
            f"This loan has already been renewed {rules['max_renewals']} time(s). "
            "It must be returned first."
        )

    new_due = date.today() + timedelta(days=rules["loan_days"])
    updated = await loans.update(loan_id, {
        "due_date": to_datetime(new_due),
        "renewed_count": int(loan.get("renewed_count") or 0) + 1,
        "status": "issued",
    })
    return {**(serialize_doc(updated) or {}), "detail": f"Renewed until {new_due:%d %b %Y}"}


async def mark_overdue(tenant: TenantContext) -> dict[str, Any]:
    """Flag loans past their due date and accrue the fine. Safe to run daily."""
    rules = _rules(tenant)
    today = date.today()
    loans = await collection(C.LIBRARY_LOANS).find({
        "tenant_id": tenant.id, "status": "issued", "is_deleted": {"$ne": True},
        "due_date": {"$lt": to_datetime(today)},
    }).to_list(length=5000)

    notified = 0
    for loan in loans:
        late_days, fine = fine_for(loan.get("due_date"), today, rules["fine_per_day"])
        await collection(C.LIBRARY_LOANS).update_one(
            {"_id": loan["_id"]},
            {"$set": {"status": "overdue", "fine_amount": fine, "updated_at": utcnow()}},
        )
        notified += await _notify_overdue(tenant, loan, late_days, fine)

    return {
        "marked_overdue": len(loans),
        "notified": notified,
        "detail": f"{len(loans)} loan(s) marked overdue",
    }


async def _notify_overdue(
    tenant: TenantContext, loan: dict[str, Any], late_days: int, fine: float
) -> int:
    from app.modules.communication.notify import notify_users

    field = "staff_id" if loan.get("borrower_type") == "staff" else "student_id"
    user_ids = await collection(C.USERS).distinct("_id", {
        "tenant_id": tenant.id, field: loan["borrower_id"], "is_active": True,
    })
    if not user_ids:
        return 0
    await notify_users(
        tenant, user_ids,
        title=f"Library item overdue: {loan.get('item_title', '')}",
        body=(
            f"It is {late_days} day(s) late"
            + (f" and a fine of {fine} has accrued." if fine else ".")
        ),
        category="library",
        type="warning",
        link="/facilities/library",
    )
    return len(user_ids)


async def borrower_history(
    tenant: TenantContext, borrower_id: str
) -> dict[str, Any]:
    loans = await collection(C.LIBRARY_LOANS).find({
        "tenant_id": tenant.id, "borrower_id": ObjectId(borrower_id),
        "is_deleted": {"$ne": True},
    }).sort([("issued_on", -1)]).to_list(length=200)

    outstanding = money(sum(
        float(loan.get("fine_amount") or 0)
        for loan in loans
        if not loan.get("fine_paid") and not loan.get("fine_waived")
    ))
    return {
        "loans": [serialize_doc(loan) for loan in loans],
        "currently_out": sum(1 for loan in loans if loan.get("status") in {"issued", "overdue"}),
        "outstanding_fines": outstanding,
    }


async def dashboard(tenant: TenantContext) -> dict[str, Any]:
    base = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    items = await collection(C.LIBRARY_ITEMS).aggregate([
        {"$match": base},
        {"$group": {"_id": None, "titles": {"$sum": 1},
                    "copies": {"$sum": "$total_copies"},
                    "available": {"$sum": "$available_copies"}}},
    ]).to_list(length=1)
    stock = items[0] if items else {"titles": 0, "copies": 0, "available": 0}

    today = to_datetime(date.today())
    return {
        "titles": stock["titles"],
        "total_copies": stock["copies"],
        "available_copies": stock["available"],
        "on_loan": await collection(C.LIBRARY_LOANS).count_documents(
            {**base, "status": {"$in": ["issued", "overdue"]}}
        ),
        "overdue": await collection(C.LIBRARY_LOANS).count_documents(
            {**base, "status": "issued", "due_date": {"$lt": today}}
        ) + await collection(C.LIBRARY_LOANS).count_documents({**base, "status": "overdue"}),
        "due_today": await collection(C.LIBRARY_LOANS).count_documents({
            **base, "status": "issued",
            "due_date": {"$gte": today, "$lt": today + timedelta(days=1)},
        }),
    }
