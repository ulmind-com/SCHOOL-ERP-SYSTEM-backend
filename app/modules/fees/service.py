"""Fee invoicing and collection.

Two rules shape everything here:

* An invoice is never edited once money has touched it — corrections are new
  lines or a credit, so the paper trail matches the receipt book.
* Receipt numbers come from an atomic per-institution counter, never from a
  count of existing rows, so two cashiers collecting at once cannot collide.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.core.exceptions import Conflict, NotFound, ValidationError
from app.db.mongo import C, collection
from app.db.repository import Repository, next_sequence
from app.models.base import serialize_doc, to_datetime, utcnow
from app.models.finance import INSTALMENTS, money

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

#: A run bills one cycle at a time. "all" is the escape hatch for institutions
#: that raise a single invoice for the whole year and settle it in one go.
BILLING_CYCLES = ("all", "one_time", "monthly", "quarterly", "half_yearly", "semester", "yearly")


def period_label_for(cycle: str, on: date, index: int | None = None) -> str:
    """The name a parent sees on the invoice.

    Derived from the cycle rather than typed, so two runs for the same month
    produce the same label — which is what stops a student being billed twice.
    """
    if cycle == "monthly":
        return f"{MONTH_NAMES[on.month - 1]} {on.year}"
    if cycle == "quarterly":
        return f"Quarter {index or ((on.month - 1) // 3) + 1} · {on.year}"
    if cycle == "half_yearly":
        return f"{'First' if (index or (1 if on.month <= 6 else 2)) == 1 else 'Second'} Half · {on.year}"
    if cycle == "semester":
        return f"Semester {index or (1 if on.month <= 6 else 2)} · {on.year}"
    if cycle == "one_time":
        return f"One-time charges · {on.year}"
    return f"Full Year {on.year}"


def _due_date_for(cycle: str, components: list[dict], on: date, fallback: date) -> date:
    """Honour the structure's own due day where the components agree on one."""
    days = {int(c.get("due_day") or 0) for c in components if c.get("due_day")}
    if cycle != "monthly" or len(days) != 1:
        return fallback
    day = min(max(days.pop(), 1), 28)
    try:
        return on.replace(day=day)
    except ValueError:
        return fallback


# ── Numbering ─────────────────────────────────────────────────────────────
async def next_invoice_number(tenant: TenantContext) -> str:
    conf = (tenant.settings or {}).get("invoice_number", {})
    prefix = conf.get("prefix", "INV")
    year = datetime.now(UTC).year
    seq = await next_sequence(tenant.id, f"invoice:{year}")
    return f"{prefix}-{year}-{str(seq).zfill(5)}"


async def next_receipt_number(tenant: TenantContext) -> str:
    conf = (tenant.settings or {}).get("receipt_number", {})
    prefix = conf.get("prefix", "RCP")
    year = datetime.now(UTC).year
    seq = await next_sequence(tenant.id, f"receipt:{year}")
    return f"{prefix}-{year}-{str(seq).zfill(5)}"


# ── Discounts ─────────────────────────────────────────────────────────────
async def applicable_discounts(
    tenant_id: ObjectId, student_id: ObjectId, class_id: ObjectId | None
) -> list[dict[str, Any]]:
    today = to_datetime(date.today())
    return await collection(C.DISCOUNTS).find({
        "tenant_id": tenant_id,
        "status": "active",
        "is_deleted": {"$ne": True},
        "$or": [{"student_id": student_id}, {"class_ids": class_id}],
        "$and": [
            {"$or": [{"valid_from": None}, {"valid_from": {"$lte": today}}]},
            {"$or": [{"valid_till": None}, {"valid_till": {"$gte": today}}]},
        ],
    }).to_list(length=20)


def discount_for_line(
    discounts: list[dict[str, Any]], fee_head_id: ObjectId | None, amount: float
) -> float:
    """Concessions stack additively but can never exceed the line itself."""
    total = 0.0
    for discount in discounts:
        heads = discount.get("fee_head_ids") or []
        if heads and fee_head_id not in heads:
            continue
        value = float(discount.get("value") or 0)
        total += amount * min(value, 100) / 100 if discount.get("type") == "percentage" else value
    return money(min(total, amount))


# ── Invoice generation ────────────────────────────────────────────────────
def instalment_labels(frequency: str, start_month: int) -> list[tuple[str, int]]:
    """Period labels and the month each instalment falls in."""
    count = INSTALMENTS.get(frequency, 1)  # type: ignore[arg-type]
    if count == 1:
        return [("Full Year", start_month)]
    step = 12 // count
    out = []
    for i in range(count):
        month = (start_month - 1 + i * step) % 12 + 1
        label = MONTH_NAMES[month - 1] if count == 12 else f"{_ordinal(i + 1)} Instalment"
        out.append((label, month))
    return out


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


async def generate_invoices(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    fee_structure_id: str,
    academic_year_id: str,
    class_id: str | None = None,
    section_id: str | None = None,
    student_ids: list[str] | None = None,
    period_label: str | None = None,
    due_date: date | None = None,
    cycle: str = "all",
    period_index: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Raise one invoice per student for a billing period.

    ``cycle`` picks which components are billed: a monthly run raises only the
    monthly lines, a semester run only the semester lines. That is what lets one
    structure carry a monthly tuition, a yearly lab fee and a one-time admission
    charge without any of them being billed on the wrong schedule.

    Re-running for the same period is safe: a student who already has an
    invoice for that label is skipped rather than double-billed.
    """
    if cycle not in BILLING_CYCLES:
        raise ValidationError(f"Billing cycle must be one of: {', '.join(BILLING_CYCLES)}")
    structure = await collection(C.FEE_STRUCTURES).find_one(
        {"_id": ObjectId(fee_structure_id), "tenant_id": tenant.id,
         "is_deleted": {"$ne": True}}
    )
    if structure is None:
        raise NotFound("Fee structure not found")

    query: dict[str, Any] = {"tenant_id": tenant.id, "status": "active",
                             "is_deleted": {"$ne": True}}
    if student_ids:
        query["_id"] = {"$in": [ObjectId(s) for s in student_ids if ObjectId.is_valid(s)]}
    elif section_id:
        query["current_section_id"] = ObjectId(section_id)
    elif class_id:
        query["current_class_id"] = ObjectId(class_id)
    elif structure.get("class_ids"):
        query["current_class_id"] = {"$in": structure["class_ids"]}
    else:
        raise ValidationError("Choose a class, section or list of students to bill")

    students = await collection(C.STUDENTS).find(query).to_list(length=5000)
    if not students:
        raise ValidationError("No active students matched that selection")

    today = date.today()
    label = period_label or period_label_for(cycle, today, period_index)
    billable = [
        component
        for component in structure.get("components", [])
        if cycle == "all" or (component.get("frequency") or "monthly") == cycle
    ]
    if not billable:
        raise ValidationError(
            f"'{structure.get('name', 'This structure')}' has no {cycle.replace('_', ' ')} "
            "components to bill. Pick a different cycle."
        )
    due = due_date or _due_date_for(
        cycle, billable, today, today + timedelta(days=15)
    )
    year_id = ObjectId(academic_year_id)
    invoices = Repository(C.FEE_INVOICES, tenant.id, actor_id=auth.user_id)

    created, skipped, preview = 0, 0, []
    total_billed = 0.0

    for student in students:
        exists = await invoices.exists({
            "student_id": student["_id"], "period_label": label,
            "academic_year_id": year_id,
            "status": {"$nin": ["cancelled"]},
        })
        if exists:
            skipped += 1
            continue

        discounts = await applicable_discounts(
            tenant.id, student["_id"], student.get("current_class_id")
        )
        concession = float(student.get("fee_concession_percent") or 0)

        lines, subtotal, discount_total = [], 0.0, 0.0
        for component in billable:
            if not _student_is_billed_for(student, component):
                continue
            amount = money(component.get("amount"))
            head_id = component.get("fee_head_id")
            head_oid = ObjectId(head_id) if head_id and ObjectId.is_valid(str(head_id)) else None
            line_discount = discount_for_line(discounts, head_oid, amount)
            if concession:
                line_discount = money(min(amount, line_discount + amount * concession / 100))
            lines.append({
                "fee_head_id": str(head_id or ""),
                "description": component.get("fee_head_name") or "Fee",
                "amount": amount,
                "discount": line_discount,
                "tax": 0.0,
                "net": money(amount - line_discount),
            })
            subtotal += amount
            discount_total += line_discount

        total = money(subtotal - discount_total)
        total_billed += total

        if dry_run:
            preview.append({
                "student_id": str(student["_id"]),
                "name": " ".join(filter(None, [student.get("first_name"),
                                               student.get("last_name")])),
                "subtotal": money(subtotal),
                "discount": money(discount_total),
                "total": total,
            })
            continue

        await invoices.create({
            "number": await next_invoice_number(tenant),
            "student_id": student["_id"],
            "class_id": student.get("current_class_id"),
            "section_id": student.get("current_section_id"),
            "academic_year_id": year_id,
            "fee_structure_id": structure["_id"],
            "period_label": label,
            "issue_date": utcnow(),
            "due_date": to_datetime(due),
            "lines": lines,
            "subtotal": money(subtotal),
            "discount_total": money(discount_total),
            "tax_total": 0.0,
            "late_fee": 0.0,
            "total": total,
            "paid_amount": 0.0,
            "status": "issued",
        })
        created += 1
        await _refresh_outstanding(tenant.id, student["_id"])

    return {
        "dry_run": dry_run,
        "cycle": cycle,
        "period_label": label,
        "due_date": due.isoformat(),
        "matched": len(students),
        "created": created,
        "skipped_existing": skipped,
        "total_billed": money(total_billed),
        "preview": preview[:100],
        "detail": (
            f"{len(students)} student(s) would be billed {money(total_billed)}"
            if dry_run else f"{created} invoice(s) raised, {skipped} already existed"
        ),
    }


def _student_is_billed_for(student: dict[str, Any], component: dict[str, Any]) -> bool:
    """Whether this student should be charged this line.

    Facility lines follow the facility, not the fee structure: a school puts
    transport in the Class 8 structure because *some* of Class 8 takes the bus,
    and billing the walkers would be wrong regardless of how the component is
    flagged. Everything else bills unless explicitly marked optional.
    """
    name = (component.get("fee_head_name") or "").lower()
    if "transport" in name or "bus" in name:
        return bool(student.get("uses_transport"))
    if "hostel" in name or "boarding" in name:
        return bool(student.get("is_hosteller"))
    return not component.get("is_optional", False)


# ── Collection ────────────────────────────────────────────────────────────
async def collect_payment(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    student_id: str,
    amount: float,
    method: str = "cash",
    invoice_id: str | None = None,
    reference: str = "",
    bank_name: str = "",
    paid_at: datetime | None = None,
    remarks: str = "",
) -> dict[str, Any]:
    """Take money against one invoice, or spread it over the oldest dues."""
    amount = money(amount)
    if amount <= 0:
        raise ValidationError("Enter an amount greater than zero")

    sid = ObjectId(student_id)
    student = await collection(C.STUDENTS).find_one(
        {"_id": sid, "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if student is None:
        raise NotFound("Student not found")

    invoices_repo = Repository(C.FEE_INVOICES, tenant.id, actor_id=auth.user_id)
    if invoice_id:
        targets = [await invoices_repo.get_or_404(invoice_id, label="Invoice")]
    else:
        targets = await invoices_repo.list(
            {"student_id": sid, "status": {"$in": ["issued", "partially_paid", "overdue"]}},
            sort_by="due_date", sort_dir="asc",
        )
    if not targets:
        raise ValidationError("This student has no outstanding invoice")

    remaining = amount
    allocations = []
    for invoice in targets:
        if remaining <= 0:
            break
        balance = money(float(invoice.get("total", 0)) - float(invoice.get("paid_amount", 0)))
        if balance <= 0:
            continue
        applied = money(min(balance, remaining))
        new_paid = money(float(invoice.get("paid_amount", 0)) + applied)
        status = "paid" if new_paid >= float(invoice.get("total", 0)) else "partially_paid"
        await invoices_repo.update(invoice["_id"], {"paid_amount": new_paid, "status": status})
        allocations.append({
            "invoice_id": str(invoice["_id"]),
            "invoice_number": invoice.get("number", ""),
            "applied": applied,
            "status": status,
        })
        remaining = money(remaining - applied)

    if not allocations:
        raise Conflict("Those invoices are already settled")

    receipt = await next_receipt_number(tenant)
    payment = await Repository(C.PAYMENTS, tenant.id, actor_id=auth.user_id).create({
        "receipt_number": receipt,
        "student_id": sid,
        "invoice_id": ObjectId(allocations[0]["invoice_id"]),
        "invoice_number": allocations[0]["invoice_number"],
        "amount": money(amount - remaining),
        "method": method,
        "reference": reference,
        "bank_name": bank_name,
        "paid_at": paid_at or utcnow(),
        "collected_by": auth.user_id,
        "academic_year_id": student.get("academic_year_id"),
        "status": "success",
        "remarks": remarks,
        "allocations": allocations,
    })
    await _refresh_outstanding(tenant.id, sid)

    return {
        "receipt_number": receipt,
        "payment_id": str(payment["_id"]),
        "collected": money(amount - remaining),
        "unallocated": remaining,
        "allocations": allocations,
        "detail": (
            f"Receipt {receipt} for {money(amount - remaining)}"
            + (f" — {remaining} left unallocated (no further dues)" if remaining else "")
        ),
    }


async def _refresh_outstanding(tenant_id: ObjectId, student_id: ObjectId) -> None:
    rows = await collection(C.FEE_INVOICES).aggregate([
        {"$match": {"tenant_id": tenant_id, "student_id": student_id,
                    "is_deleted": {"$ne": True},
                    "status": {"$in": ["issued", "partially_paid", "overdue"]}}},
        {"$group": {"_id": None, "due": {"$sum": {"$subtract": ["$total", "$paid_amount"]}}}},
    ]).to_list(length=1)
    outstanding = money(rows[0]["due"]) if rows else 0.0
    await collection(C.STUDENTS).update_one(
        {"_id": student_id}, {"$set": {"outstanding_amount": outstanding}}
    )


async def mark_overdue(tenant: TenantContext) -> dict[str, Any]:
    """Flip past-due invoices and apply late fees. Safe to run daily."""
    today = to_datetime(date.today())
    invoices = collection(C.FEE_INVOICES)
    due = await invoices.find({
        "tenant_id": tenant.id, "is_deleted": {"$ne": True},
        "status": {"$in": ["issued", "partially_paid"]},
        "due_date": {"$lt": today},
    }).to_list(length=5000)

    structures = {
        s["_id"]: s
        for s in await collection(C.FEE_STRUCTURES).find({"tenant_id": tenant.id}).to_list(None)
    }
    updated = 0
    for invoice in due:
        structure = structures.get(invoice.get("fee_structure_id")) or {}
        per_day = float(structure.get("late_fee_per_day") or 0)
        grace = int(structure.get("late_fee_grace_days") or 0)
        cap = float(structure.get("max_late_fee") or 0)

        late_fee = float(invoice.get("late_fee") or 0)
        if per_day:
            overdue_by = (today - invoice["due_date"]).days - grace
            if overdue_by > 0:
                late_fee = money(overdue_by * per_day)
                if cap:
                    late_fee = money(min(late_fee, cap))

        base = money(float(invoice.get("subtotal", 0)) - float(invoice.get("discount_total", 0)))
        await invoices.update_one(
            {"_id": invoice["_id"]},
            {"$set": {"status": "overdue", "late_fee": late_fee,
                      "total": money(base + late_fee), "updated_at": utcnow()}},
        )
        updated += 1
    return {"marked_overdue": updated, "detail": f"{updated} invoice(s) marked overdue"}


# ── Reporting ─────────────────────────────────────────────────────────────
async def collection_summary(
    tenant: TenantContext, *, start: date | None = None, end: date | None = None
) -> dict[str, Any]:
    match: dict[str, Any] = {"tenant_id": tenant.id, "is_deleted": {"$ne": True},
                             "status": "success"}
    if start and end:
        match["paid_at"] = {"$gte": to_datetime(start), "$lte": to_datetime(end) + timedelta(days=1)}

    by_method = await collection(C.PAYMENTS).aggregate([
        {"$match": match},
        {"$group": {"_id": "$method", "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
        {"$sort": {"total": -1}},
    ]).to_list(length=None)

    by_day = await collection(C.PAYMENTS).aggregate([
        {"$match": match},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$paid_at"}},
            "total": {"$sum": "$amount"}, "count": {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]).to_list(length=None)

    billed = await collection(C.FEE_INVOICES).aggregate([
        {"$match": {"tenant_id": tenant.id, "is_deleted": {"$ne": True},
                    "status": {"$nin": ["draft", "cancelled"]}}},
        {"$group": {"_id": "$status", "total": {"$sum": "$total"},
                    "paid": {"$sum": "$paid_amount"}, "count": {"$sum": 1}}},
    ]).to_list(length=None)

    total_billed = money(sum(r["total"] for r in billed))
    total_paid = money(sum(r["paid"] for r in billed))
    return {
        "collected": money(sum(r["total"] for r in by_method)),
        "transactions": sum(r["count"] for r in by_method),
        "by_method": [{"method": r["_id"], "total": money(r["total"]), "count": r["count"]}
                      for r in by_method],
        "by_day": [{"date": r["_id"], "total": money(r["total"]), "count": r["count"]}
                   for r in by_day],
        "billed": total_billed,
        "paid": total_paid,
        "outstanding": money(total_billed - total_paid),
        "collection_rate": round(total_paid / total_billed * 100, 2) if total_billed else 0.0,
        "by_status": [{"status": r["_id"], "count": r["count"], "total": money(r["total"])}
                      for r in billed],
    }


async def family_fee_account(
    tenant: TenantContext, auth: AuthContext
) -> dict[str, Any]:
    """The students a portal user may pay for, with what each owes.

    A student and a parent hold ``invoices:read`` but not ``students:read`` —
    they have no business listing the roll — so the portal cannot get the names
    it needs from the students endpoint. This hands back only their own family.
    """
    from app.core.scoping import family_student_ids

    allowed = await family_student_ids(auth, tenant)
    query: dict[str, Any] = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    if allowed is not None:
        query["_id"] = {"$in": allowed}
    elif auth.student_id:
        query["_id"] = auth.student_id

    students = await collection(C.STUDENTS).find(query).to_list(length=50)
    class_ids = [s["current_class_id"] for s in students if s.get("current_class_id")]
    classes = {
        c["_id"]: c.get("name", "")
        for c in await collection(C.CLASSES).find({"_id": {"$in": class_ids}}).to_list(None)
    }

    out = []
    for student in students:
        rows = await collection(C.FEE_INVOICES).aggregate([
            {"$match": {"tenant_id": tenant.id, "student_id": student["_id"],
                        "is_deleted": {"$ne": True},
                        "status": {"$nin": ["cancelled", "draft"]}}},
            {"$group": {"_id": None, "billed": {"$sum": "$total"},
                        "paid": {"$sum": "$paid_amount"}}},
        ]).to_list(length=1)
        data = rows[0] if rows else {}
        billed, paid = money(data.get("billed", 0)), money(data.get("paid", 0))
        out.append({
            "id": str(student["_id"]),
            "full_name": " ".join(filter(None, [student.get("first_name"),
                                                student.get("middle_name"),
                                                student.get("last_name")])),
            "admission_number": student.get("admission_number", ""),
            "class_name": classes.get(student.get("current_class_id"), ""),
            "billed": billed,
            "paid": paid,
            "outstanding": money(max(billed - paid, 0)),
        })
    out.sort(key=lambda s: s["full_name"])
    return {"students": out, "outstanding": money(sum(s["outstanding"] for s in out))}


async def student_ledger(tenant: TenantContext, student_id: str) -> dict[str, Any]:
    sid = ObjectId(student_id)
    invoices = await collection(C.FEE_INVOICES).find(
        {"tenant_id": tenant.id, "student_id": sid, "is_deleted": {"$ne": True}}
    ).sort([("issue_date", -1)]).to_list(length=200)
    payments = await collection(C.PAYMENTS).find(
        {"tenant_id": tenant.id, "student_id": sid, "is_deleted": {"$ne": True}}
    ).sort([("paid_at", -1)]).to_list(length=200)

    billed = money(sum(float(i.get("total", 0)) for i in invoices
                       if i.get("status") not in {"cancelled", "draft"}))
    paid = money(sum(float(i.get("paid_amount", 0)) for i in invoices))
    return {
        "invoices": [
            {**(serialize_doc(i) or {}),
             "balance": money(float(i.get("total", 0)) - float(i.get("paid_amount", 0)))}
            for i in invoices
        ],
        "payments": [serialize_doc(p) for p in payments],
        "totals": {"billed": billed, "paid": paid, "outstanding": money(billed - paid)},
    }
