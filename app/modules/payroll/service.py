"""Payroll.

A run is generated from three inputs: the staff member's salary structure,
their attendance for the month, and the institution's leave policy. Generating
is idempotent — re-running a draft replaces its payslips rather than adding a
second set — and an approved run is frozen, because a payslip that changes
after the money moved is worse than no payslip.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.core.exceptions import Conflict, NotFound, ValidationError
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models.base import serialize_doc, to_datetime, utcnow
from app.models.finance import money

PAID_STATUSES = {"present", "late", "half_day", "leave", "holiday", "wfh"}
FROZEN = {"approved", "paid"}


def period_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def working_days_in(year: int, month: int, *, week_off: int = 7) -> int:
    """Calendar days minus the weekly off. Indian schools commonly work six
    days, so Sunday (7) is the default rather than a two-day weekend."""
    start, end = period_bounds(year, month)
    days = 0
    current = start
    while current <= end:
        if current.isoweekday() != week_off:
            days += 1
        current = date.fromordinal(current.toordinal() + 1)
    return days


def compute_payslip(
    *,
    basic: float,
    components: list[dict[str, Any]],
    working_days: float,
    present_days: float,
    lop_days: float,
) -> dict[str, Any]:
    """Earnings, deductions and loss of pay for one person.

    Loss of pay is applied to the *gross*, not just the basic: a day not worked
    costs the allowances too, which is how every school payroll I have seen
    treats it.
    """
    earnings: list[dict[str, Any]] = [{"name": "Basic", "amount": money(basic)}]
    deductions: list[dict[str, Any]] = []

    for component in components:
        value = (
            basic * float(component.get("value") or 0) / 100
            if component.get("calculation") == "percent_of_basic"
            else float(component.get("value") or 0)
        )
        row = {"name": component.get("name", ""), "amount": money(value)}
        if component.get("type") == "deduction":
            deductions.append(row)
        else:
            earnings.append(row)

    gross = money(sum(e["amount"] for e in earnings))

    lop_amount = 0.0
    if lop_days > 0 and working_days > 0:
        lop_amount = money(gross / working_days * lop_days)
        deductions.append({"name": f"Loss of pay ({lop_days:g} day(s))", "amount": lop_amount})

    total_deductions = money(sum(d["amount"] for d in deductions))
    return {
        "basic": money(basic),
        "earnings": earnings,
        "deductions": deductions,
        "gross": gross,
        "total_deductions": total_deductions,
        "net_pay": money(gross - total_deductions),
        "working_days": working_days,
        "present_days": present_days,
        "lop_days": lop_days,
    }


async def generate(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    year: int,
    month: int,
    staff_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not 1 <= month <= 12:
        raise ValidationError("Month must be between 1 and 12")

    period = f"{year}-{month:02d}"
    runs = Repository(C.PAYROLL_RUNS, tenant.id, actor_id=auth.user_id)
    payslips = Repository(C.PAYSLIPS, tenant.id, actor_id=auth.user_id)

    run = await runs.find_one({"period": period})
    if run and run.get("status") in FROZEN:
        raise Conflict(
            f"Payroll for {period} is already {run['status']} and cannot be regenerated"
        )

    query: dict[str, Any] = {"tenant_id": tenant.id, "status": "active",
                             "is_deleted": {"$ne": True}}
    if staff_ids:
        query["_id"] = {"$in": [ObjectId(s) for s in staff_ids if ObjectId.is_valid(s)]}

    staff = await collection(C.STAFF).find(query).to_list(length=2000)
    if not staff:
        raise ValidationError("No active staff matched that selection")

    structures = {
        s["staff_id"]: s
        for s in await collection(C.SALARY_STRUCTURES).find(
            {"tenant_id": tenant.id, "is_active": True, "is_deleted": {"$ne": True}}
        ).to_list(length=None)
        if s.get("staff_id")
    }

    start, end = period_bounds(year, month)
    week_off = int((tenant.settings or {}).get("payroll", {}).get("week_off_iso_day", 7))
    working = working_days_in(year, month, week_off=week_off)

    attendance = await collection(C.STAFF_ATTENDANCE).aggregate([
        {"$match": {
            "tenant_id": tenant.id, "is_deleted": {"$ne": True},
            "date": {"$gte": to_datetime(start), "$lte": to_datetime(end)},
        }},
        {"$group": {
            "_id": "$staff_id",
            "marked": {"$sum": 1},
            "paid": {"$sum": {"$cond": [{"$in": ["$status", list(PAID_STATUSES)]}, 1, 0]}},
            "half": {"$sum": {"$cond": [{"$eq": ["$status", "half_day"]}, 1, 0]}},
            "absent": {"$sum": {"$cond": [{"$eq": ["$status", "absent"]}, 1, 0]}},
        }},
    ]).to_list(length=None)
    by_staff = {row["_id"]: row for row in attendance}

    if run:
        # Regenerating a draft: clear the old payslips rather than stack them.
        await payslips.hard_delete_many({"payroll_run_id": run["_id"]})
        run_id = run["_id"]
    else:
        created = await runs.create({
            "period": period, "month": month, "year": year, "status": "draft",
        })
        run_id = created["_id"]

    gross_total = deduction_total = net_total = 0.0
    generated = skipped = 0

    for member in staff:
        structure = structures.get(member["_id"])
        basic = float(
            (structure or {}).get("basic") or member.get("basic_salary") or 0
        )
        if basic <= 0:
            skipped += 1
            continue

        stats = by_staff.get(member["_id"])
        if stats:
            # Only days actually marked count against pay. An unmarked day is a
            # record-keeping gap, not evidence of absence.
            half_day_loss = stats["half"] * 0.5
            lop = float(stats["absent"]) + half_day_loss
            present = float(stats["paid"]) - half_day_loss
        else:
            lop, present = 0.0, float(working)

        slip = compute_payslip(
            basic=basic,
            components=(structure or {}).get("components") or [],
            working_days=working,
            present_days=present,
            lop_days=lop,
        )

        await payslips.create({
            "payroll_run_id": run_id,
            "staff_id": member["_id"],
            "staff_name": " ".join(filter(None, [member.get("first_name"),
                                                 member.get("last_name")])),
            "employee_id": member.get("employee_id", ""),
            "designation": member.get("designation", ""),
            "period": period,
            "status": "generated",
            **slip,
        })

        gross_total += slip["gross"]
        deduction_total += slip["total_deductions"]
        net_total += slip["net_pay"]
        generated += 1

    await runs.update(str(run_id), {
        "staff_count": generated,
        "gross_total": money(gross_total),
        "deduction_total": money(deduction_total),
        "net_total": money(net_total),
        "processed_by": auth.user_id,
        "processed_at": utcnow(),
        "status": "processing",
    })

    return {
        "run_id": str(run_id),
        "period": period,
        "generated": generated,
        "skipped_no_salary": skipped,
        "working_days": working,
        "gross_total": money(gross_total),
        "deduction_total": money(deduction_total),
        "net_total": money(net_total),
        "detail": (
            f"{generated} payslip(s) generated for {period}"
            + (f"; {skipped} skipped with no salary set" if skipped else "")
        ),
    }


async def approve(tenant: TenantContext, auth: AuthContext, run_id: str) -> dict[str, Any]:
    runs = Repository(C.PAYROLL_RUNS, tenant.id, actor_id=auth.user_id)
    run = await runs.get_or_404(run_id, label="Payroll run")
    if run.get("status") in FROZEN:
        raise Conflict(f"This run is already {run['status']}")
    if not run.get("staff_count"):
        raise ValidationError("Generate the payslips before approving")

    updated = await runs.update(run_id, {
        "status": "approved", "approved_by": auth.user_id, "approved_at": utcnow(),
    })
    await collection(C.PAYSLIPS).update_many(
        {"tenant_id": tenant.id, "payroll_run_id": ObjectId(run_id)},
        {"$set": {"status": "approved", "updated_at": utcnow()}},
    )
    return {**(serialize_doc(updated) or {}), "detail": "Payroll approved and frozen"}


async def mark_paid(
    tenant: TenantContext, auth: AuthContext, run_id: str, *, reference: str = ""
) -> dict[str, Any]:
    runs = Repository(C.PAYROLL_RUNS, tenant.id, actor_id=auth.user_id)
    run = await runs.get_or_404(run_id, label="Payroll run")
    if run.get("status") != "approved":
        raise Conflict("Approve the run before marking it paid")

    updated = await runs.update(run_id, {"status": "paid", "paid_at": utcnow()})
    await collection(C.PAYSLIPS).update_many(
        {"tenant_id": tenant.id, "payroll_run_id": ObjectId(run_id)},
        {"$set": {"status": "paid", "paid_on": utcnow(),
                  "payment_reference": reference, "updated_at": utcnow()}},
    )
    await _notify_paid(tenant, run_id, run.get("period", ""))
    return {**(serialize_doc(updated) or {}), "detail": "Payroll marked paid"}


async def _notify_paid(tenant: TenantContext, run_id: str, period: str) -> None:
    from app.modules.communication.notify import notify_users

    staff_ids = await collection(C.PAYSLIPS).distinct(
        "staff_id", {"tenant_id": tenant.id, "payroll_run_id": ObjectId(run_id)}
    )
    user_ids = await collection(C.USERS).distinct("_id", {
        "tenant_id": tenant.id, "staff_id": {"$in": staff_ids}, "is_active": True,
    })
    if not user_ids:
        return
    await notify_users(
        tenant, user_ids,
        title=f"Salary for {period} has been released",
        body="Your payslip is available in the portal.",
        category="payroll", type="success", link="/finance/payroll",
    )


async def run_detail(tenant: TenantContext, run_id: str) -> dict[str, Any]:
    run = await collection(C.PAYROLL_RUNS).find_one(
        {"_id": ObjectId(run_id), "tenant_id": tenant.id}
    )
    if run is None:
        raise NotFound("Payroll run not found")
    slips = await collection(C.PAYSLIPS).find(
        {"tenant_id": tenant.id, "payroll_run_id": run["_id"], "is_deleted": {"$ne": True}}
    ).sort([("staff_name", 1)]).to_list(length=2000)
    return {
        "run": serialize_doc(run),
        "payslips": [serialize_doc(s) for s in slips],
    }
