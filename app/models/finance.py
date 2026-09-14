"""Fees, invoicing, collection, expenses and payroll.

Money is stored as a float with an explicit currency on the institution rather
than as minor units — Indian fee books are quoted in rupees with two decimals
and every figure here is rounded at the point of write, not at display.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, computed_field

from app.models.base import AppModel, FileRef, PyObjectId, TenantDocument


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"
    WAIVED = "waived"


class PaymentMethod(StrEnum):
    CASH = "cash"
    CARD = "card"
    UPI = "upi"
    NET_BANKING = "net_banking"
    CHEQUE = "cheque"
    DD = "dd"
    BANK_TRANSFER = "bank_transfer"
    ONLINE = "online"


class Frequency(StrEnum):
    ONE_TIME = "one_time"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    HALF_YEARLY = "half_yearly"
    SEMESTER = "semester"
    YEARLY = "yearly"


#: How many times a year a component at each frequency is billed. Semester and
#: half-yearly both bill twice but are kept apart because colleges label the
#: period "Semester 1", not "H1", and the label is what a parent reads.
INSTALMENTS = {
    Frequency.ONE_TIME: 1,
    Frequency.MONTHLY: 12,
    Frequency.QUARTERLY: 4,
    Frequency.HALF_YEARLY: 2,
    Frequency.SEMESTER: 2,
    Frequency.YEARLY: 1,
}


class FeeHead(TenantDocument):
    """A line a school can charge for — tuition, transport, lab."""

    code: str
    name: str
    description: str = ""
    category: str = "academic"      # academic | facility | one_time | penalty | optional
    is_recurring: bool = True
    is_optional: bool = False
    is_refundable: bool = False
    default_amount: float = 0
    is_active: bool = True


class FeeComponent(AppModel):
    fee_head_id: str
    fee_head_name: str = ""
    #: Shown to the family verbatim, so they can see what they are paying for.
    description: str = ""
    amount: float = 0
    frequency: Frequency = Frequency.MONTHLY
    is_optional: bool = False
    due_day: int = 10               # day of month an instalment falls due

    # ── When this charge can actually be paid ─────────────────────────────
    #: Days before the due date that collection opens. ``None`` means it is
    #: open as soon as the invoice exists — the usual case.
    opens_days_before: int | None = None
    #: Days after the due date that collection closes. ``None`` means it stays
    #: open until it is paid, which is what a school wants for tuition.
    closes_days_after: int | None = None
    #: Fixed dates, for a one-off charge tied to a real event — an examination
    #: fee collected for the fortnight before the exam. These win over the
    #: relative offsets above when set.
    collect_from: datetime | None = None
    collect_until: datetime | None = None


class FeeStructure(TenantDocument):
    """What a given class pays in a given year."""

    name: str
    academic_year_id: PyObjectId
    #: A school bills by class; a college bills by programme or department, and
    #: the same structure has to serve both. Any of these matching is enough.
    class_ids: list[PyObjectId] = Field(default_factory=list)
    program_ids: list[PyObjectId] = Field(default_factory=list)
    department_ids: list[PyObjectId] = Field(default_factory=list)
    #: Which semester this applies to, for a college that charges differently as
    #: a course progresses. Empty means every semester.
    semesters: list[int] = Field(default_factory=list)
    #: Kept for structures written before the lists above existed.
    program_id: PyObjectId | None = None
    components: list[FeeComponent] = Field(default_factory=list)
    late_fee_per_day: float = 0
    late_fee_grace_days: int = 7
    max_late_fee: float = 0
    is_active: bool = True
    notes: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def annual_total(self) -> float:
        return round(
            sum(c.amount * INSTALMENTS.get(c.frequency, 1) for c in self.components), 2
        )


class InvoiceLine(AppModel):
    fee_head_id: str = ""
    description: str
    amount: float = 0
    discount: float = 0
    tax: float = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net(self) -> float:
        return round(self.amount - self.discount + self.tax, 2)


class FeeInvoice(TenantDocument):
    number: str
    student_id: PyObjectId
    class_id: PyObjectId | None = None
    section_id: PyObjectId | None = None
    academic_year_id: PyObjectId | None = None
    fee_structure_id: PyObjectId | None = None

    period_label: str = ""          # "April 2026", "Term 1"
    issue_date: datetime | None = None
    due_date: datetime | None = None

    lines: list[InvoiceLine] = Field(default_factory=list)
    subtotal: float = 0
    discount_total: float = 0
    tax_total: float = 0
    late_fee: float = 0
    total: float = 0
    paid_amount: float = 0
    status: InvoiceStatus = InvoiceStatus.ISSUED

    notes: str = ""
    cancelled_reason: str = ""
    reminders_sent: int = 0
    last_reminder_at: datetime | None = None
    pdf: FileRef | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def balance(self) -> float:
        return round(max(self.total - self.paid_amount, 0), 2)


class Payment(TenantDocument):
    receipt_number: str
    student_id: PyObjectId
    invoice_id: PyObjectId | None = None
    invoice_number: str = ""
    amount: float = 0
    method: PaymentMethod = PaymentMethod.CASH
    reference: str = ""             # UPI ref / cheque no / gateway txn id
    bank_name: str = ""
    paid_at: datetime | None = None
    collected_by: PyObjectId | None = None
    academic_year_id: PyObjectId | None = None
    status: str = "success"         # success | pending | failed | refunded
    refunded_amount: float = 0
    refund_reason: str = ""
    remarks: str = ""
    receipt_pdf: FileRef | None = None


class Discount(TenantDocument):
    """A concession or scholarship applied to a student's fees."""

    code: str
    name: str
    student_id: PyObjectId | None = None
    class_ids: list[PyObjectId] = Field(default_factory=list)
    fee_head_ids: list[PyObjectId] = Field(default_factory=list)
    type: str = "percentage"        # percentage | fixed
    value: float = 0
    reason: str = ""                # sibling | staff ward | merit | need-based | RTE
    academic_year_id: PyObjectId | None = None
    valid_from: datetime | None = None
    valid_till: datetime | None = None
    status: str = "active"          # draft | active | expired | revoked
    approved_by: PyObjectId | None = None
    approved_at: datetime | None = None
    documents: list[FileRef] = Field(default_factory=list)

    def apply_to(self, amount: float) -> float:
        if self.type == "percentage":
            return round(amount * min(self.value, 100) / 100, 2)
        return round(min(self.value, amount), 2)


class Expense(TenantDocument):
    title: str
    category: str = "general"       # salary | utilities | maintenance | supplies | events …
    amount: float = 0
    tax: float = 0
    vendor: str = ""
    invoice_number: str = ""
    spent_on: datetime | None = None
    paid_via: PaymentMethod = PaymentMethod.BANK_TRANSFER
    department_id: PyObjectId | None = None
    status: str = "pending"         # pending | approved | rejected | paid
    approved_by: PyObjectId | None = None
    approved_at: datetime | None = None
    rejection_reason: str = ""
    attachments: list[FileRef] = Field(default_factory=list)
    notes: str = ""


class SalaryComponent(AppModel):
    name: str
    type: str = "earning"           # earning | deduction
    calculation: str = "fixed"      # fixed | percent_of_basic
    value: float = 0
    is_taxable: bool = True


class SalaryStructure(TenantDocument):
    name: str = ""
    staff_id: PyObjectId | None = None
    designation: str = ""
    basic: float = 0
    components: list[SalaryComponent] = Field(default_factory=list)
    effective_from: datetime | None = None
    is_active: bool = True

    def compute(self) -> dict[str, float]:
        earnings = self.basic
        deductions = 0.0
        for component in self.components:
            value = (
                self.basic * component.value / 100
                if component.calculation == "percent_of_basic"
                else component.value
            )
            if component.type == "earning":
                earnings += value
            else:
                deductions += value
        return {
            "gross": round(earnings, 2),
            "deductions": round(deductions, 2),
            "net": round(earnings - deductions, 2),
        }


class PayrollRun(TenantDocument):
    period: str                     # "2026-09"
    month: int = 1
    year: int = 2026
    status: str = "draft"           # draft | processing | approved | paid | cancelled
    staff_count: int = 0
    gross_total: float = 0
    deduction_total: float = 0
    net_total: float = 0
    processed_by: PyObjectId | None = None
    processed_at: datetime | None = None
    approved_by: PyObjectId | None = None
    approved_at: datetime | None = None
    paid_at: datetime | None = None
    notes: str = ""


class Payslip(TenantDocument):
    payroll_run_id: PyObjectId
    staff_id: PyObjectId
    period: str = ""
    working_days: float = 0
    present_days: float = 0
    leave_days: float = 0
    lop_days: float = 0
    basic: float = 0
    earnings: list[dict] = Field(default_factory=list)
    deductions: list[dict] = Field(default_factory=list)
    gross: float = 0
    total_deductions: float = 0
    net_pay: float = 0
    status: str = "generated"       # generated | approved | paid
    paid_on: datetime | None = None
    payment_reference: str = ""
    pdf: FileRef | None = None


def money(value: float | int | None) -> float:
    return round(float(value or 0), 2)
