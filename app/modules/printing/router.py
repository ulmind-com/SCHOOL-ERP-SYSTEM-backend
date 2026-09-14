"""Printable documents, generated from live records."""

from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, Request, Response

from app.core.context import AuthContext, TenantContext
from app.core.deps import CurrentUser, TenantDep, require
from app.core.exceptions import Forbidden, NotFound
from app.db.mongo import C, collection
from app.modules.printing import pdf
from app.utils.audit import record

router = APIRouter(prefix="/print", tags=["Documents"])


def _pdf(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


async def _class_names(tenant: TenantContext, class_id, section_id=None) -> tuple[str, str]:
    school_class = await collection(C.CLASSES).find_one({"_id": class_id}) if class_id else None
    section = await collection(C.SECTIONS).find_one({"_id": section_id}) if section_id else None
    return (school_class or {}).get("name", ""), (section or {}).get("name", "")


async def _student_or_403(
    tenant: TenantContext, auth: AuthContext, student_id: ObjectId
) -> dict:
    """Families may print their own children's documents and nobody else's."""
    if auth.student_id and auth.student_id != student_id:
        raise Forbidden("You can only print your own documents")
    if auth.guardian_id:
        guardian = await collection(C.GUARDIANS).find_one(
            {"_id": auth.guardian_id, "tenant_id": tenant.id}
        )
        if student_id not in ((guardian or {}).get("student_ids") or []):
            raise Forbidden("You can only print your own children's documents")

    student = await collection(C.STUDENTS).find_one(
        {"_id": student_id, "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if student is None:
        raise NotFound("Student not found")
    return student


@router.get("/receipt/{payment_id}", summary="Fee receipt")
async def receipt(payment_id: str, auth: CurrentUser, tenant: TenantDep):
    payment = await collection(C.PAYMENTS).find_one(
        {"_id": ObjectId(payment_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if payment is None:
        raise NotFound("Payment not found")

    student = await _student_or_403(tenant, auth, payment["student_id"])
    invoice = (
        await collection(C.FEE_INVOICES).find_one({"_id": payment["invoice_id"]})
        if payment.get("invoice_id") else None
    )
    class_name, _ = await _class_names(tenant, student.get("current_class_id"))

    return _pdf(
        pdf.fee_receipt(tenant, payment=payment, student=student, invoice=invoice,
                        class_name=class_name),
        f"receipt-{payment.get('receipt_number', payment_id)}.pdf",
    )


@router.get("/invoice/{invoice_id}", summary="Fee invoice")
async def invoice(invoice_id: str, auth: CurrentUser, tenant: TenantDep):
    doc = await collection(C.FEE_INVOICES).find_one(
        {"_id": ObjectId(invoice_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if doc is None:
        raise NotFound("Invoice not found")
    student = await _student_or_403(tenant, auth, doc["student_id"])
    class_name, _ = await _class_names(tenant, doc.get("class_id"))
    return _pdf(
        pdf.invoice_pdf(tenant, invoice=doc, student=student, class_name=class_name),
        f"invoice-{doc.get('number', invoice_id)}.pdf",
    )


@router.get("/report-card/{student_id}/{exam_id}", summary="Report card")
async def report_card(student_id: str, exam_id: str, auth: CurrentUser, tenant: TenantDep):
    student = await _student_or_403(tenant, auth, ObjectId(student_id))
    exam = await collection(C.EXAMS).find_one(
        {"_id": ObjectId(exam_id), "tenant_id": tenant.id}
    )
    if exam is None:
        raise NotFound("Exam not found")

    marks = await collection(C.MARKS).find({
        "tenant_id": tenant.id, "student_id": ObjectId(student_id),
        "exam_id": ObjectId(exam_id), "is_deleted": {"$ne": True},
    }).to_list(length=100)
    if not marks:
        raise NotFound("No marks have been entered for this student in this exam")

    subject_names = {
        s["_id"]: s.get("name", "")
        for s in await collection(C.SUBJECTS).find({"tenant_id": tenant.id}).to_list(None)
    }
    scale = await collection(C.GRADE_SCALES).find_one(
        {"tenant_id": tenant.id, "is_default": True}
    )
    bands = (scale or {}).get("bands") or []

    def grade_for(percentage: float) -> str:
        for band in bands:
            if float(band["min"]) <= percentage <= float(band["max"]):
                return band.get("grade", "")
        return ""

    subjects, obtained, maximum = [], 0.0, 0.0
    for mark in marks:
        mark_max = float(mark.get("max_marks") or 0)
        mark_got = float(mark.get("marks_obtained") or 0)
        percentage = round(mark_got / mark_max * 100, 2) if mark_max else 0
        subjects.append({
            "subject_name": subject_names.get(mark["subject_id"], ""),
            "max_marks": mark_max,
            "marks_obtained": mark_got,
            "percentage": percentage,
            "grade": mark.get("grade") or grade_for(percentage),
            "is_pass": mark.get("is_pass", True),
        })
        obtained += mark_got
        maximum += mark_max
    subjects.sort(key=lambda s: s["subject_name"])

    overall = round(obtained / maximum * 100, 2) if maximum else 0
    pass_mark = float((scale or {}).get("pass_percentage") or 33)

    from app.modules.people.service import attendance_summary

    class_name, section_name = await _class_names(
        tenant, student.get("current_class_id"), student.get("current_section_id")
    )
    return _pdf(
        pdf.report_card(
            tenant, student=student, exam=exam, subjects=subjects,
            totals={
                "obtained": obtained, "max": maximum, "percentage": overall,
                "grade": grade_for(overall),
                "result": "pass" if overall >= pass_mark and all(
                    s["is_pass"] for s in subjects
                ) else "fail",
            },
            class_name=class_name, section_name=section_name,
            attendance=await attendance_summary(tenant.id, ObjectId(student_id)),
        ),
        f"report-card-{student.get('admission_number', student_id)}.pdf",
    )


@router.get("/certificate/{certificate_id}", summary="Certificate")
async def certificate(
    certificate_id: str,
    auth: Annotated[AuthContext, Depends(require("certificates:read"))],
    tenant: TenantDep,
    request: Request,
):
    doc = await collection(C.CERTIFICATES).find_one(
        {"_id": ObjectId(certificate_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if doc is None:
        raise NotFound("Certificate not found")

    student = {}
    class_name = ""
    if doc.get("student_id"):
        student = await collection(C.STUDENTS).find_one({"_id": doc["student_id"]}) or {}
        class_name, _ = await _class_names(tenant, student.get("current_class_id"))

    await record(auth, "certificates.printed", entity_type="certificates",
                 entity_id=certificate_id, entity_label=doc.get("serial_number", ""),
                 request=request)
    return _pdf(
        pdf.certificate(tenant, record=doc, student=student, class_name=class_name),
        f"certificate-{doc.get('serial_number', certificate_id)}.pdf",
    )


@router.get("/payslip/{payslip_id}", summary="Payslip")
async def payslip(payslip_id: str, auth: CurrentUser, tenant: TenantDep):
    slip = await collection(C.PAYSLIPS).find_one(
        {"_id": ObjectId(payslip_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if slip is None:
        raise NotFound("Payslip not found")
    # Staff may print their own; anyone else needs payroll access.
    if slip.get("staff_id") != auth.staff_id and not auth.can("payroll:read"):
        raise Forbidden("You can only print your own payslip")

    staff = await collection(C.STAFF).find_one({"_id": slip["staff_id"]}) or {}
    return _pdf(
        pdf.payslip(tenant, slip=slip, staff=staff),
        f"payslip-{slip.get('period', '')}-{slip.get('employee_id', '')}.pdf",
    )


@router.get("/id-card/{student_id}", summary="Student identity slip")
async def id_card(student_id: str, auth: CurrentUser, tenant: TenantDep):
    student = await _student_or_403(tenant, auth, ObjectId(student_id))
    class_name, section_name = await _class_names(
        tenant, student.get("current_class_id"), student.get("current_section_id")
    )
    return _pdf(
        pdf.id_card(tenant, student=student, class_name=class_name,
                    section_name=section_name),
        f"id-{student.get('admission_number', student_id)}.pdf",
    )
