"""PDF documents.

ReportLab, deliberately: it is pure Python, so a Render deploy needs no system
packages — unlike the HTML-to-PDF engines, which need a browser or Cairo and
turn a one-click deploy into a Dockerfile.

Everything shares `_frame`, which draws the institution's letterhead and a
footer with a verification line. A printed receipt that cannot be traced back
to a record is not much of a receipt.
"""

from __future__ import annotations

import io
import logging
import pathlib
from datetime import date, datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.core.context import TenantContext

log = logging.getLogger("scholarly.pdf")

# Matches the product's palette so a printed page belongs to the same product.
INK = colors.HexColor("#111214")
MUTED = colors.HexColor("#8C9096")
LINE = colors.HexColor("#EAECEF")
SUNKEN = colors.HexColor("#F7F8F9")
BUTTER = colors.HexColor("#FAEE7C")

# ── Fonts ─────────────────────────────────────────────────────────────────
# ReportLab's built-in Type1 fonts are Latin-1 only, so "₹" (U+20B9, added to
# Unicode in 2010) renders as a filled box. If a Unicode TTF is available we
# register it and use the symbol; otherwise every amount reads "Rs." — which is
# what Indian receipts printed before the symbol existed, and is never tofu.
#
# To get the symbol, drop any Unicode TTF at backend/fonts/body.ttf.
FONT_BODY = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
HAS_RUPEE_GLYPH = False


RUPEE = "\u20b9"


def _has_glyph(font: Any, codepoint: int) -> bool:
    """Whether the face actually draws this character.

    Registering a TTF is not the same as it containing the glyph. Arial Unicode
    MS, the obvious macOS candidate, is from 2001 and predates the rupee sign
    (encoded in 2010) — embed it and every amount prints as an empty box. The
    character map is the only honest answer.
    """
    try:
        mapping = font.face.charToGlyph
        return bool(mapping.get(codepoint))
    except Exception:
        return False


def _register_fonts() -> None:
    global FONT_BODY, FONT_BOLD, HAS_RUPEE_GLYPH

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    bundled = pathlib.Path(__file__).resolve().parents[3] / "fonts"
    candidates = [
        (bundled / "body.ttf", bundled / "body-bold.ttf"),
        (pathlib.Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         pathlib.Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        (pathlib.Path("/Library/Fonts/Arial Unicode.ttf"), None),
        (pathlib.Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"), None),
    ]
    for regular, bold in candidates:
        if not regular.exists():
            continue
        try:
            face = TTFont("Body", str(regular))
            pdfmetrics.registerFont(face)
            bold_name = "Body"
            if bold and bold.exists():
                pdfmetrics.registerFont(TTFont("Body-Bold", str(bold)))
                bold_name = "Body-Bold"
            FONT_BODY, FONT_BOLD = "Body", bold_name
            HAS_RUPEE_GLYPH = _has_glyph(face, 0x20B9)
            log.info(
                "PDF fonts: %s registered (rupee glyph: %s)",
                regular.name, "yes" if HAS_RUPEE_GLYPH else "no, using 'Rs.'",
            )
            return
        except Exception as exc:  # a broken font must not stop a receipt printing
            log.warning("Could not register %s: %s", regular, exc)
    log.info("PDF fonts: no Unicode TTF found, amounts will print as 'Rs.'")


_register_fonts()

_base = getSampleStyleSheet()

STYLES = {
    "title": ParagraphStyle("title", parent=_base["Title"], fontName=FONT_BOLD,
                            fontSize=18, leading=22, textColor=INK, spaceAfter=2),
    "subtitle": ParagraphStyle("subtitle", parent=_base["Normal"], fontName=FONT_BODY, fontSize=9,
                               textColor=MUTED, alignment=TA_CENTER, spaceAfter=10),
    "h2": ParagraphStyle("h2", parent=_base["Heading2"], fontName=FONT_BOLD,
                         fontSize=11.5, textColor=INK, spaceBefore=10, spaceAfter=5),
    "body": ParagraphStyle("body", parent=_base["Normal"], fontName=FONT_BODY, fontSize=9.5, leading=14,
                           textColor=INK),
    "small": ParagraphStyle("small", parent=_base["Normal"], fontName=FONT_BODY, fontSize=8,
                            textColor=MUTED, leading=11),
    "right": ParagraphStyle("right", parent=_base["Normal"], fontName=FONT_BODY, fontSize=9.5,
                            alignment=TA_RIGHT, textColor=INK),
    "centre": ParagraphStyle("centre", parent=_base["Normal"], fontName=FONT_BODY, fontSize=10, leading=16,
                             alignment=TA_CENTER, textColor=INK),
}


def _money(value: Any, currency: str = "INR") -> str:
    if currency == "INR":
        symbol = "₹" if HAS_RUPEE_GLYPH else "Rs."
    else:
        symbol = {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency, f"{currency} ")
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return f"{symbol}0"
    # Indian digit grouping: 12,34,567.89
    whole, _, frac = f"{abs(amount):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join([*parts, tail])
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol}{whole}.{frac}"


def _fmt(value: Any, fallback: str = "—") -> str:
    if value in (None, ""):
        return fallback
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y")
        except ValueError:
            return value
    return str(value)


def _letterhead(tenant: TenantContext, document_title: str) -> list:
    strap = "<br/>".join(filter(None, [tenant.address_line, tenant.contact_line]))
    return [
        Paragraph(tenant.name, STYLES["title"]),
        Paragraph(strap or tenant.slug, STYLES["subtitle"]),
        HRFlowable(width="100%", thickness=1.2, color=INK, spaceAfter=10),
        Paragraph(document_title.upper(), ParagraphStyle(
            "doctitle", parent=STYLES["h2"], alignment=TA_CENTER, fontSize=12,
            spaceBefore=0, spaceAfter=12,
        )),
    ]


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(FONT_BODY, 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 12 * mm, doc.footer_note)
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


def _build(flowables: list, footer_note: str) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=20 * mm,
        title=footer_note,
    )
    document.footer_note = footer_note
    document.build(flowables, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def _kv_table(rows: list[tuple[str, str]], widths=(45 * mm, 55 * mm)) -> Table:
    table = Table([[Paragraph(k, STYLES["small"]), Paragraph(v, STYLES["body"])]
                   for k, v in rows], colWidths=widths)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _data_table(header: list[str], rows: list[list[str]], widths=None,
                aligns: dict[int, str] | None = None) -> Table:
    table = Table([header, *rows], colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
        ("FONTNAME", (0, 1), (-1, -1), FONT_BODY),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SUNKEN]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    for index, align in (aligns or {}).items():
        style.append(("ALIGN", (index, 0), (index, -1), align.upper()))
    table.setStyle(TableStyle(style))
    return table


def _signature_block(labels: list[str]) -> Table:
    cells = [[Paragraph(f"<br/><br/>_______________________<br/>{label}",
                        ParagraphStyle("sig", parent=STYLES["small"], alignment=TA_CENTER))
              for label in labels]]
    table = Table(cells, colWidths=[(174 * mm) / len(labels)] * len(labels))
    table.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 18)]))
    return table


# ── Documents ─────────────────────────────────────────────────────────────
def fee_receipt(
    tenant: TenantContext, *, payment: dict[str, Any], student: dict[str, Any],
    invoice: dict[str, Any] | None = None, class_name: str = "",
) -> bytes:
    currency = tenant.currency or "INR"
    story = _letterhead(tenant, "Fee Receipt")

    student_name = " ".join(filter(None, [student.get("first_name"),
                                          student.get("last_name")]))
    header = Table([[
        _kv_table([
            ("Receipt No.", payment.get("receipt_number", "")),
            ("Date", _fmt(payment.get("paid_at"))),
            ("Mode", str(payment.get("method", "")).replace("_", " ").title()),
            ("Reference", payment.get("reference") or "—"),
        ]),
        _kv_table([
            ("Student", student_name),
            ("Admission No.", student.get("admission_number", "")),
            ("Class", class_name or "—"),
            ("Roll No.", student.get("roll_number") or "—"),
        ]),
    ]], colWidths=[87 * mm, 87 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(header)
    story.append(Spacer(1, 12))

    lines = (invoice or {}).get("lines") or []
    if lines:
        story.append(Paragraph("Fee details", STYLES["h2"]))
        story.append(_data_table(
            ["Particulars", "Amount", "Discount", "Net"],
            [[
                line.get("description", ""),
                _money(line.get("amount"), currency),
                _money(line.get("discount"), currency),
                _money(line.get("net", line.get("amount")), currency),
            ] for line in lines],
            widths=[84 * mm, 30 * mm, 30 * mm, 30 * mm],
            aligns={1: "right", 2: "right", 3: "right"},
        ))
        story.append(Spacer(1, 8))

    summary_rows = [["Amount paid", _money(payment.get("amount"), currency)]]
    if invoice:
        balance = float(invoice.get("total", 0)) - float(invoice.get("paid_amount", 0))
        summary_rows.insert(0, ["Invoice total", _money(invoice.get("total"), currency)])
        summary_rows.append(["Balance", _money(max(balance, 0), currency)])

    totals = Table(summary_rows, colWidths=[120 * mm, 54 * mm])
    totals.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, 0), (-1, -1), FONT_BODY),
        ("FONTNAME", (0, len(summary_rows) - 1), (-1, len(summary_rows) - 1), FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LINEABOVE", (0, len(summary_rows) - 1), (-1, len(summary_rows) - 1), 0.8, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(totals)

    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"Received with thanks. This is a computer-generated receipt "
        f"for {_money(payment.get('amount'), currency)}.", STYLES["small"]
    ))
    story.append(_signature_block(["Received by", "Authorised signatory"]))

    return _build(story, f"{tenant.name} · Receipt {payment.get('receipt_number', '')}")


def report_card(
    tenant: TenantContext, *, student: dict[str, Any], exam: dict[str, Any],
    subjects: list[dict[str, Any]], totals: dict[str, Any],
    class_name: str = "", section_name: str = "", attendance: dict[str, Any] | None = None,
) -> bytes:
    story = _letterhead(tenant, f"Report Card — {exam.get('name', '')}")

    student_name = " ".join(filter(None, [student.get("first_name"),
                                          student.get("last_name")]))
    header = Table([[
        _kv_table([
            ("Student", student_name),
            ("Admission No.", student.get("admission_number", "")),
            ("Class", f"{class_name} {section_name}".strip() or "—"),
        ]),
        _kv_table([
            ("Roll No.", student.get("roll_number") or "—"),
            ("Examination", exam.get("name", "")),
            ("Date", _fmt(exam.get("end_date")) or _fmt(date.today())),
        ]),
    ]], colWidths=[87 * mm, 87 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(header)
    story.append(Spacer(1, 12))

    story.append(_data_table(
        ["Subject", "Max", "Obtained", "%", "Grade", "Result"],
        [[
            subject.get("subject_name", ""),
            f"{subject.get('max_marks', 0):g}",
            f"{subject.get('marks_obtained', 0):g}",
            f"{subject.get('percentage', 0):.1f}",
            subject.get("grade", "—"),
            "Pass" if subject.get("is_pass", True) else "Fail",
        ] for subject in subjects],
        widths=[62 * mm, 22 * mm, 26 * mm, 22 * mm, 22 * mm, 20 * mm],
        aligns={1: "center", 2: "center", 3: "center", 4: "center", 5: "center"},
    ))
    story.append(Spacer(1, 10))

    summary = [
        ("Total marks", f"{totals.get('obtained', 0):g} / {totals.get('max', 0):g}"),
        ("Percentage", f"{totals.get('percentage', 0):.2f}%"),
        ("Grade", totals.get("grade", "—")),
        ("Result", totals.get("result", "—").title()),
    ]
    if totals.get("rank"):
        summary.append(("Rank in class", str(totals["rank"])))
    if attendance:
        summary.append(("Attendance", f"{attendance.get('percentage', 0):.1f}%"))

    box = Table([[_kv_table(summary, widths=(40 * mm, 45 * mm))]], colWidths=[174 * mm])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SUNKEN),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(box)

    if totals.get("remark"):
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"<b>Remark:</b> {totals['remark']}", STYLES["body"]))

    story.append(_signature_block(["Class teacher", "Principal", "Parent"]))
    return _build(story, f"{tenant.name} · {student_name} · {exam.get('name', '')}")


def certificate(
    tenant: TenantContext, *, record: dict[str, Any], student: dict[str, Any],
    class_name: str = "",
) -> bytes:
    kind = str(record.get("type", "bonafide")).replace("_", " ").title()
    story = _letterhead(tenant, record.get("title") or f"{kind} Certificate")

    student_name = " ".join(filter(None, [student.get("first_name"),
                                          student.get("last_name")]))
    body = record.get("content") or _default_certificate_text(
        kind, student_name, student, class_name, tenant
    )

    story.append(Spacer(1, 10))
    story.append(Paragraph(body.replace("\n", "<br/>"), ParagraphStyle(
        "cert", parent=STYLES["centre"], fontSize=11, leading=20,
    )))
    story.append(Spacer(1, 18))
    story.append(_kv_table([
        ("Serial No.", record.get("serial_number", "")),
        ("Issued on", _fmt(record.get("issued_on")) or _fmt(date.today())),
        ("Valid till", _fmt(record.get("valid_till"))),
    ], widths=(40 * mm, 60 * mm)))
    story.append(_signature_block(["Principal", "Authorised signatory"]))

    return _build(story, f"{tenant.name} · {kind} · {record.get('serial_number', '')}")


def _default_certificate_text(
    kind: str, student_name: str, student: dict[str, Any], class_name: str,
    tenant: TenantContext,
) -> str:
    admission = student.get("admission_number", "")
    if kind.lower().startswith("transfer"):
        return (
            f"This is to certify that <b>{student_name}</b> (Admission No. {admission}) "
            f"was a bona fide student of {tenant.name}, studying in {class_name or 'this institution'}."
            f"<br/><br/>The student has cleared all dues and left the institution "
            f"with a satisfactory record of conduct."
        )
    if kind.lower().startswith("character"):
        return (
            f"This is to certify that <b>{student_name}</b> (Admission No. {admission}) "
            f"has been a student of {tenant.name}."
            f"<br/><br/>To the best of our knowledge, the student bears a good moral character."
        )
    return (
        f"This is to certify that <b>{student_name}</b> (Admission No. {admission}) "
        f"is a bona fide student of {tenant.name}"
        + (f", currently studying in {class_name}." if class_name else ".")
        + "<br/><br/>This certificate is issued on request for official purposes."
    )


def payslip(
    tenant: TenantContext, *, slip: dict[str, Any], staff: dict[str, Any]
) -> bytes:
    currency = tenant.currency or "INR"
    story = _letterhead(tenant, f"Payslip — {slip.get('period', '')}")

    staff_name = slip.get("staff_name") or " ".join(
        filter(None, [staff.get("first_name"), staff.get("last_name")])
    )
    header = Table([[
        _kv_table([
            ("Employee", staff_name),
            ("Employee ID", slip.get("employee_id") or staff.get("employee_id", "")),
            ("Designation", slip.get("designation") or staff.get("designation", "")),
        ]),
        _kv_table([
            ("Period", slip.get("period", "")),
            ("Working days", f"{slip.get('working_days', 0):g}"),
            ("Loss of pay", f"{slip.get('lop_days', 0):g} day(s)"),
        ]),
    ]], colWidths=[87 * mm, 87 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(header)
    story.append(Spacer(1, 12))

    earnings = slip.get("earnings") or []
    deductions = slip.get("deductions") or []
    rows = max(len(earnings), len(deductions))
    table_rows = []
    for index in range(rows):
        earning = earnings[index] if index < len(earnings) else {}
        deduction = deductions[index] if index < len(deductions) else {}
        table_rows.append([
            earning.get("name", ""),
            _money(earning.get("amount"), currency) if earning else "",
            deduction.get("name", ""),
            _money(deduction.get("amount"), currency) if deduction else "",
        ])
    table_rows.append([
        "Gross earnings", _money(slip.get("gross"), currency),
        "Total deductions", _money(slip.get("total_deductions"), currency),
    ])

    table = _data_table(
        ["Earnings", "Amount", "Deductions", "Amount"], table_rows,
        widths=[52 * mm, 35 * mm, 52 * mm, 35 * mm],
        aligns={1: "right", 3: "right"},
    )
    table.setStyle(TableStyle([
        ("FONTNAME", (0, len(table_rows)), (-1, len(table_rows)), FONT_BOLD),
        ("LINEABOVE", (0, len(table_rows)), (-1, len(table_rows)), 0.8, INK),
    ]))
    story.append(table)
    story.append(Spacer(1, 10))

    net = Table([["NET PAY", _money(slip.get("net_pay"), currency)]],
                colWidths=[120 * mm, 54 * mm])
    net.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BUTTER),
        ("FONTNAME", (0, 0), (-1, -1), FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(net)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "This is a computer-generated payslip and does not require a signature.",
        STYLES["small"],
    ))
    return _build(story, f"{tenant.name} · Payslip {slip.get('period', '')} · {staff_name}")


def invoice_pdf(
    tenant: TenantContext, *, invoice: dict[str, Any], student: dict[str, Any],
    class_name: str = "",
) -> bytes:
    currency = tenant.currency or "INR"
    story = _letterhead(tenant, "Fee Invoice")

    student_name = " ".join(filter(None, [student.get("first_name"),
                                          student.get("last_name")]))
    header = Table([[
        _kv_table([
            ("Invoice No.", invoice.get("number", "")),
            ("Issued", _fmt(invoice.get("issue_date"))),
            ("Due", _fmt(invoice.get("due_date"))),
            ("Period", invoice.get("period_label", "")),
        ]),
        _kv_table([
            ("Student", student_name),
            ("Admission No.", student.get("admission_number", "")),
            ("Class", class_name or "—"),
            ("Status", str(invoice.get("status", "")).replace("_", " ").title()),
        ]),
    ]], colWidths=[87 * mm, 87 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(header)
    story.append(Spacer(1, 12))

    story.append(_data_table(
        ["Particulars", "Amount", "Discount", "Net"],
        [[
            line.get("description", ""),
            _money(line.get("amount"), currency),
            _money(line.get("discount"), currency),
            _money(line.get("net", line.get("amount")), currency),
        ] for line in (invoice.get("lines") or [])],
        widths=[84 * mm, 30 * mm, 30 * mm, 30 * mm],
        aligns={1: "right", 2: "right", 3: "right"},
    ))
    story.append(Spacer(1, 8))

    balance = float(invoice.get("total", 0)) - float(invoice.get("paid_amount", 0))
    rows = [
        ["Subtotal", _money(invoice.get("subtotal"), currency)],
        ["Discount", f"- {_money(invoice.get('discount_total'), currency)}"],
    ]
    if float(invoice.get("late_fee") or 0) > 0:
        rows.append(["Late fee", _money(invoice.get("late_fee"), currency)])
    rows += [
        ["Total", _money(invoice.get("total"), currency)],
        ["Paid", _money(invoice.get("paid_amount"), currency)],
        ["Balance due", _money(max(balance, 0), currency)],
    ]

    totals = Table(rows, colWidths=[120 * mm, 54 * mm])
    totals.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("FONTNAME", (0, 0), (-1, -1), FONT_BODY),
        ("FONTNAME", (0, len(rows) - 1), (-1, len(rows) - 1), FONT_BOLD),
        ("LINEABOVE", (0, len(rows) - 3), (-1, len(rows) - 3), 0.8, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(totals)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Please quote the invoice number when paying. "
        "Fees are payable by the due date shown above.", STYLES["small"],
    ))
    return _build(story, f"{tenant.name} · Invoice {invoice.get('number', '')}")


def id_card(
    tenant: TenantContext, *, student: dict[str, Any], class_name: str = "",
    section_name: str = "",
) -> bytes:
    """A printable sheet of identity details. Photographs are not embedded —
    they live in ImageKit and fetching them here would make PDF generation
    depend on a network call that can fail."""
    story = _letterhead(tenant, "Student Identity Slip")
    student_name = " ".join(filter(None, [student.get("first_name"),
                                          student.get("last_name")]))
    guardian = (student.get("emergency_contact_name")
                or (student.get("medical") or {}).get("emergency_contact_name", ""))

    story.append(KeepTogether([
        _kv_table([
            ("Name", student_name),
            ("Admission No.", student.get("admission_number", "")),
            ("Class", f"{class_name} {section_name}".strip() or "—"),
            ("Roll No.", student.get("roll_number") or "—"),
            ("Date of birth", _fmt(student.get("date_of_birth"))),
            ("Blood group", (student.get("medical") or {}).get("blood_group") or "—"),
            ("Phone", (student.get("contact") or {}).get("phone") or "—"),
            ("Emergency contact", guardian or "—"),
            ("Address", (student.get("address") or {}).get("city") or "—"),
        ], widths=(45 * mm, 110 * mm)),
    ]))
    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "If found, please return to the institution at the address above.",
        STYLES["small"],
    ))
    return _build(story, f"{tenant.name} · ID · {student.get('admission_number', '')}")
