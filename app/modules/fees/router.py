"""Fees — invoices, collection and finance reporting."""

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.context import AuthContext
from app.core.crud import Resource, build_crud_router
from app.core.deps import TenantDep, require
from app.core.scoping import assert_may_see_student, student_row_scope
from app.db.mongo import C
from app.models import finance as fin
from app.models.base import AppModel
from app.modules.fees import service
from app.utils.audit import record

router = APIRouter()

INVOICES = Resource(
    name="invoices", collection=C.FEE_INVOICES, module="invoices", model=fin.FeeInvoice,
    label="Invoice", tags=["Finance"], search_fields=["number", "period_label"],
    filters=["student_id", "class_id", "section_id", "status", "academic_year_id",
             "fee_structure_id", "period_label"],
    sortable=["due_date", "issue_date", "created_at", "total"],
    unique_fields=["number"], generated_fields=["number"],
    # Students and parents hold invoices:read for their own bills; without this
    # the same permission would list the whole school's.
    scope_hook=student_row_scope,
)
PAYMENTS = Resource(
    name="payments", collection=C.PAYMENTS, module="payments", model=fin.Payment,
    label="Payment", tags=["Finance"], search_fields=["receipt_number", "reference"],
    filters=["student_id", "invoice_id", "method", "status", "academic_year_id",
             "collected_by"],
    sortable=["paid_at", "created_at", "amount"],
    unique_fields=["receipt_number"], generated_fields=["receipt_number"],
    read_only=True,  # money moves through /fees/collect, never a bare POST
    scope_hook=student_row_scope,
)
PAYROLL_RUNS = Resource(
    name="payroll/runs", collection=C.PAYROLL_RUNS, module="payroll", model=fin.PayrollRun,
    label="Payroll Run", plural="Payroll Runs", tags=["Payroll"],
    search_fields=["period"], filters=["status", "year", "month"], sortable=["period"],
    unique_fields=["period"],
)
PAYSLIPS = Resource(
    name="payroll/payslips", collection=C.PAYSLIPS, module="payroll", model=fin.Payslip,
    label="Payslip", tags=["Payroll"], filters=["payroll_run_id", "staff_id", "status",
                                                "period"],
)

for resource in (INVOICES, PAYMENTS, PAYROLL_RUNS, PAYSLIPS):
    router.include_router(build_crud_router(resource))


fees = APIRouter(prefix="/fees", tags=["Finance"])

Reader = Annotated[AuthContext, Depends(require("invoices:read"))]
Biller = Annotated[AuthContext, Depends(require("invoices:create"))]
Collector = Annotated[AuthContext, Depends(require("payments:collect"))]
Analyst = Annotated[AuthContext, Depends(require("payments:read"))]


class GenerateInvoicesRequest(AppModel):
    fee_structure_id: str
    academic_year_id: str
    class_id: str | None = None
    section_id: str | None = None
    student_ids: list[str] | None = None
    period_label: str | None = None
    due_date: date | None = None
    #: Which components to bill — "monthly", "semester", "yearly"… or "all" for
    #: a single invoice covering every component in the structure.
    cycle: str = "all"
    #: Which instalment of that cycle this is (Quarter 2, Semester 1). Only used
    #: to build the period label; today's date decides when it is omitted.
    period_index: int | None = None
    dry_run: bool = False


class CollectPaymentRequest(AppModel):
    student_id: str
    amount: float
    method: str = "cash"
    invoice_id: str | None = None
    reference: str = ""
    bank_name: str = ""
    paid_at: datetime | None = None
    remarks: str = ""


@fees.post("/generate-invoices", summary="Raise invoices for a class or cohort")
async def generate(
    payload: GenerateInvoicesRequest, auth: Biller, tenant: TenantDep, request: Request
):
    """Set ``dry_run`` to preview who would be billed and for how much before
    committing — the step most fee runs are missing when they go wrong."""
    result = await service.generate_invoices(
        tenant, auth,
        fee_structure_id=payload.fee_structure_id,
        academic_year_id=payload.academic_year_id,
        class_id=payload.class_id,
        section_id=payload.section_id,
        student_ids=payload.student_ids,
        period_label=payload.period_label,
        due_date=payload.due_date,
        cycle=payload.cycle,
        period_index=payload.period_index,
        dry_run=payload.dry_run,
    )
    if not payload.dry_run:
        await record(auth, "fees.invoices_generated", entity_type="invoices",
                     changes={"created": result["created"],
                              "billed": result["total_billed"]}, request=request)
    return result


@fees.post("/collect", summary="Collect a payment and issue a receipt")
async def collect(
    payload: CollectPaymentRequest, auth: Collector, tenant: TenantDep, request: Request
):
    result = await service.collect_payment(
        tenant, auth,
        student_id=payload.student_id,
        amount=payload.amount,
        method=payload.method,
        invoice_id=payload.invoice_id,
        reference=payload.reference,
        bank_name=payload.bank_name,
        paid_at=payload.paid_at,
        remarks=payload.remarks,
    )
    await record(auth, "fees.payment_collected", entity_type="payments",
                 entity_id=result["payment_id"], entity_label=result["receipt_number"],
                 changes={"amount": result["collected"]}, request=request)
    return result


@fees.get("/ledger/{student_id}", summary="A student's invoices and receipts")
async def ledger(student_id: str, auth: Reader, tenant: TenantDep):
    await assert_may_see_student(auth, tenant, student_id)
    return await service.student_ledger(tenant, student_id)


@fees.get("/summary", summary="Collection and outstanding overview")
async def summary(
    auth: Analyst, tenant: TenantDep, start: date | None = None, end: date | None = None
):
    return await service.collection_summary(tenant, start=start, end=end)


@fees.post("/mark-overdue", summary="Flag past-due invoices and apply late fees")
async def overdue(auth: Biller, tenant: TenantDep, request: Request):
    result = await service.mark_overdue(tenant)
    await record(auth, "fees.mark_overdue", entity_type="invoices",
                 changes={"count": result["marked_overdue"]}, request=request)
    return result


router.include_router(fees)
