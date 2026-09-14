"""A populated demo institution, so the product can be judged with data in it.

Deliberately small but *shaped* like a real school: two classes with sections,
subjects, staff, students with guardians, a fortnight of attendance, a fee
structure with invoices and part-payments, and an exam with marks.
"""

from __future__ import annotations

import os
import random
import secrets
from datetime import UTC, date, datetime, timedelta

from bson import ObjectId

from app.db.mongo import C, collection
from app.models.base import to_datetime, utcnow
from app.modules.tenants.provisioning import provision_tenant

random.seed(20260914)  # reproducible demo data

FIRST_NAMES_F = ["Riya", "Sana", "Ananya", "Ishita", "Meera", "Priya", "Tanvi", "Aditi",
                 "Kavya", "Neha", "Sneha", "Diya"]
FIRST_NAMES_M = ["Arjun", "Dev", "Rohan", "Aryan", "Kabir", "Vivaan", "Ishaan", "Aarav",
                 "Rudra", "Krish", "Manav", "Yash"]
LAST_NAMES = ["Das", "Mehta", "Khan", "Roy", "Sharma", "Banerjee", "Iyer", "Chatterjee",
              "Gupta", "Nair", "Bose", "Mukherjee"]

STAFF = [
    ("Sunita", "Rao", "Senior Teacher", ["Mathematics"]),
    ("Amit", "Verma", "Teacher", ["Science"]),
    ("Priyanka", "Ghosh", "Teacher", ["English"]),
    ("Rajesh", "Kumar", "Head of Department", ["Social Studies"]),
    ("Farida", "Sheikh", "Teacher", ["Hindi"]),
    ("Nikhil", "Joshi", "Accountant", []),
]

SUBJECTS = [
    ("MATH", "Mathematics", 100, 33),
    ("SCI", "Science", 100, 33),
    ("ENG", "English", 100, 33),
    ("SST", "Social Studies", 100, 33),
    ("HIN", "Hindi", 100, 33),
    ("COMP", "Computer Science", 50, 17),
]


def _stamp(tenant_id: ObjectId) -> dict:
    return {
        "tenant_id": tenant_id,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "is_deleted": False,
    }


async def seed_demo() -> dict:
    """Idempotent-ish: if the demo institution already exists, it is reused and
    only missing pieces are added."""
    slug = "demo-valley-school"
    existing = await collection(C.TENANTS).find_one({"slug": slug})

    if existing:
        tenant_id = existing["_id"]
        owner_password = "(unchanged — set when the demo was first seeded)"
    else:
        # Generated, never hardcoded. This repository is public, and a literal
        # here would be a known password on every deployment that ever runs
        # `seed --demo`. Override with DEMO_OWNER_PASSWORD if you want a fixed
        # one for a shared sandbox.
        owner_password = os.getenv("DEMO_OWNER_PASSWORD") or (
            f"Demo-{secrets.token_urlsafe(9)}"
        )
        result = await provision_tenant(
            name="Valley International School",
            slug=slug,
            owner_email="head@valley.demo",
            owner_name="Dr. Kalpana Menon",
            owner_password=owner_password,
            institution_type="school",
            plan_key="scale",
            deployment="saas",
            contact={"email": "office@valley.demo", "phone": "03340001234"},
            address={"city": "Kolkata", "state": "West Bengal", "country": "India"},
        )
        tenant_id = result["tenant_id"]

    year = await collection(C.ACADEMIC_YEARS).find_one(
        {"tenant_id": tenant_id, "is_current": True}
    )
    year_id = year["_id"]
    stamp = _stamp(tenant_id)

    # ── Staff ─────────────────────────────────────────────────────────────
    staff_ids: dict[str, ObjectId] = {}
    for index, (first, last, designation, subjects) in enumerate(STAFF, start=1):
        employee_id = f"EMP{index:04d}"
        doc = await collection(C.STAFF).find_one(
            {"tenant_id": tenant_id, "employee_id": employee_id}
        )
        if doc is None:
            doc_id = (await collection(C.STAFF).insert_one({
                **stamp,
                "employee_id": employee_id,
                "first_name": first,
                "last_name": last,
                "designation": designation,
                "employment_type": "full_time",
                "is_teaching": bool(subjects),
                "status": "active",
                "gender": "female" if first in {"Sunita", "Priyanka", "Farida"} else "male",
                "joining_date": to_datetime(date(2021, 6, 1)),
                "basic_salary": 42000 + index * 3000,
                "contact": {"phone": f"98300{index:05d}", "email": f"{first.lower()}@valley.demo"},
            })).inserted_id
        else:
            doc_id = doc["_id"]
        staff_ids[f"{first} {last}"] = doc_id

    # ── Classes, sections, subjects ───────────────────────────────────────
    class_ids: dict[str, ObjectId] = {}
    section_ids: list[tuple[ObjectId, ObjectId, str]] = []

    for level in (7, 8):
        name = f"Class {level}"
        doc = await collection(C.CLASSES).find_one(
            {"tenant_id": tenant_id, "name": name, "academic_year_id": year_id}
        )
        class_id = doc["_id"] if doc else (await collection(C.CLASSES).insert_one({
            **stamp, "name": name, "numeric_level": level, "academic_year_id": year_id,
            "capacity": 80, "is_active": True, "order": level,
        })).inserted_id
        class_ids[name] = class_id

        for section_name in ("A", "B"):
            section = await collection(C.SECTIONS).find_one(
                {"tenant_id": tenant_id, "class_id": class_id, "name": section_name}
            )
            section_id = section["_id"] if section else (
                await collection(C.SECTIONS).insert_one({
                    **stamp, "class_id": class_id, "name": section_name,
                    "academic_year_id": year_id, "capacity": 40, "current_strength": 0,
                    "room": f"R-{level}0{1 if section_name == 'A' else 2}", "is_active": True,
                })
            ).inserted_id
            section_ids.append((class_id, section_id, f"{name} {section_name}"))

        for order, (code, subject_name, max_marks, pass_marks) in enumerate(SUBJECTS, start=1):
            full_code = f"{code}{level}"
            if await collection(C.SUBJECTS).find_one({"tenant_id": tenant_id, "code": full_code}):
                continue
            await collection(C.SUBJECTS).insert_one({
                **stamp, "code": full_code, "name": subject_name, "class_id": class_id,
                "type": "core", "max_marks": max_marks, "pass_marks": pass_marks,
                "credits": 4, "is_graded": True, "is_active": True, "order": order,
            })

    # ── Students & guardians ──────────────────────────────────────────────
    existing_students = await collection(C.STUDENTS).count_documents(
        {"tenant_id": tenant_id, "is_deleted": {"$ne": True}}
    )
    created_students = 0
    if existing_students < 40:
        counter = existing_students
        for class_id, section_id, label in section_ids:
            for roll in range(1, 11):
                counter += 1
                female = roll % 2 == 0
                first = random.choice(FIRST_NAMES_F if female else FIRST_NAMES_M)
                last = random.choice(LAST_NAMES)
                student_id = (await collection(C.STUDENTS).insert_one({
                    **stamp,
                    "admission_number": f"VIS2026{counter:04d}",
                    "roll_number": str(roll),
                    "first_name": first,
                    "last_name": last,
                    "gender": "female" if female else "male",
                    "date_of_birth": to_datetime(
                        date(2012 - (1 if "8" in label else 0), random.randint(1, 12),
                             random.randint(1, 28))
                    ),
                    "current_class_id": class_id,
                    "current_section_id": section_id,
                    "academic_year_id": year_id,
                    "admission_date": to_datetime(date(2026, 4, random.randint(1, 20))),
                    "status": "active",
                    "uses_transport": roll % 3 == 0,
                    "is_hosteller": False,
                    "contact": {"phone": f"90000{counter:05d}"},
                    "address": {"city": "Kolkata", "state": "West Bengal"},
                })).inserted_id
                created_students += 1

                guardian_id = (await collection(C.GUARDIANS).insert_one({
                    **stamp,
                    "full_name": f"{random.choice(FIRST_NAMES_M)} {last}",
                    "relation": "father",
                    "student_ids": [student_id],
                    "occupation": random.choice(
                        ["Engineer", "Shopkeeper", "Doctor", "Teacher", "Driver", "Clerk"]
                    ),
                    "contact": {"phone": f"91000{counter:05d}"},
                    "is_emergency_contact": True,
                })).inserted_id
                await collection(C.STUDENTS).update_one(
                    {"_id": student_id},
                    {"$set": {"guardian_ids": [guardian_id], "primary_guardian_id": guardian_id}},
                )

        for _, section_id, _ in section_ids:
            count = await collection(C.STUDENTS).count_documents(
                {"tenant_id": tenant_id, "current_section_id": section_id, "status": "active"}
            )
            await collection(C.SECTIONS).update_one(
                {"_id": section_id}, {"$set": {"current_strength": count}}
            )

    # ── Two weeks of attendance ───────────────────────────────────────────
    attendance_written = 0
    if not await collection(C.ATTENDANCE).find_one({"tenant_id": tenant_id}):
        today = date.today()
        for _, section_id, _ in section_ids:
            students = await collection(C.STUDENTS).find(
                {"tenant_id": tenant_id, "current_section_id": section_id}
            ).to_list(length=None)
            for offset in range(14, 0, -1):
                day = today - timedelta(days=offset)
                if day.isoweekday() == 7:
                    continue
                when = datetime(day.year, day.month, day.day, tzinfo=UTC)
                records, counts = [], {"present": 0, "absent": 0, "late": 0}
                for student in students:
                    roll = random.random()
                    status = "absent" if roll < 0.07 else "late" if roll < 0.13 else "present"
                    counts[status] += 1
                    records.append({
                        **stamp, "student_id": student["_id"], "section_id": section_id,
                        "class_id": student.get("current_class_id"), "date": when,
                        "session_key": "day", "status": status,
                        "academic_year_id": year_id,
                    })
                if records:
                    await collection(C.ATTENDANCE).insert_many(records)
                    attendance_written += len(records)
                    await collection(C.ATTENDANCE_SESSIONS).insert_one({
                        **stamp, "section_id": section_id, "date": when, "session_key": "day",
                        "class_id": records[0]["class_id"], "academic_year_id": year_id,
                        "taken_at": when, "total": len(records),
                        "present": counts["present"], "absent": counts["absent"],
                        "late": counts["late"], "on_leave": 0, "is_locked": False,
                    })

    # ── Fee structure, invoices, part payments ────────────────────────────
    heads = {
        h["code"]: h
        for h in await collection(C.FEE_HEADS).find({"tenant_id": tenant_id}).to_list(length=None)
    }
    structure = await collection(C.FEE_STRUCTURES).find_one({"tenant_id": tenant_id})
    if structure is None and heads:
        structure_id = (await collection(C.FEE_STRUCTURES).insert_one({
            **stamp,
            "name": "Standard Fees 2026-27",
            "academic_year_id": year_id,
            "class_ids": list(class_ids.values()),
            "components": [
                {"fee_head_id": str(heads["TUITION"]["_id"]), "fee_head_name": "Tuition Fee",
                 "amount": 3200, "frequency": "monthly", "is_optional": False, "due_day": 10},
                {"fee_head_id": str(heads["EXAM"]["_id"]), "fee_head_name": "Examination Fee",
                 "amount": 900, "frequency": "half_yearly", "is_optional": False, "due_day": 10},
                {"fee_head_id": str(heads["LIBRARY"]["_id"]), "fee_head_name": "Library Fee",
                 "amount": 400, "frequency": "yearly", "is_optional": False, "due_day": 10},
                {"fee_head_id": str(heads["TRANSPORT"]["_id"]), "fee_head_name": "Transport Fee",
                 "amount": 1500, "frequency": "monthly", "is_optional": True, "due_day": 10},
            ],
            "late_fee_per_day": 20, "late_fee_grace_days": 5, "max_late_fee": 500,
            "is_active": True,
        })).inserted_id
    else:
        structure_id = structure["_id"] if structure else None

    invoices_created = 0
    payments_created = 0
    if structure_id and not await collection(C.FEE_INVOICES).find_one({"tenant_id": tenant_id}):
        from app.core.context import TenantContext
        from app.modules.fees.service import next_invoice_number, next_receipt_number

        tenant_doc = await collection(C.TENANTS).find_one({"_id": tenant_id})
        ctx = TenantContext(id=tenant_id, slug=slug, name=tenant_doc.get("name", ""),
                            settings=tenant_doc.get("settings") or {})

        students = await collection(C.STUDENTS).find(
            {"tenant_id": tenant_id, "status": "active"}
        ).to_list(length=None)

        for student in students:
            lines = [
                {"fee_head_id": "", "description": "Tuition Fee", "amount": 3200.0,
                 "discount": 0.0, "tax": 0.0, "net": 3200.0},
                {"fee_head_id": "", "description": "Examination Fee", "amount": 900.0,
                 "discount": 0.0, "tax": 0.0, "net": 900.0},
            ]
            if student.get("uses_transport"):
                lines.append({"fee_head_id": "", "description": "Transport Fee",
                              "amount": 1500.0, "discount": 0.0, "tax": 0.0, "net": 1500.0})
            total = sum(line["net"] for line in lines)

            roll = random.random()
            paid = total if roll < 0.45 else round(total * 0.5, 2) if roll < 0.7 else 0.0
            status = "paid" if paid >= total else "partially_paid" if paid else "issued"

            invoice_id = (await collection(C.FEE_INVOICES).insert_one({
                **stamp,
                "number": await next_invoice_number(ctx),
                "student_id": student["_id"],
                "class_id": student.get("current_class_id"),
                "section_id": student.get("current_section_id"),
                "academic_year_id": year_id,
                "fee_structure_id": structure_id,
                "period_label": "September 2026",
                "issue_date": to_datetime(date(2026, 9, 1)),
                "due_date": to_datetime(date(2026, 9, 15)),
                "lines": lines,
                "subtotal": total, "discount_total": 0.0, "tax_total": 0.0, "late_fee": 0.0,
                "total": total, "paid_amount": paid, "status": status,
            })).inserted_id
            invoices_created += 1

            if paid:
                await collection(C.PAYMENTS).insert_one({
                    **stamp,
                    "receipt_number": await next_receipt_number(ctx),
                    "student_id": student["_id"],
                    "invoice_id": invoice_id,
                    "amount": paid,
                    "method": random.choice(["cash", "upi", "net_banking", "cheque"]),
                    "paid_at": to_datetime(date(2026, 9, random.randint(2, 14))),
                    "academic_year_id": year_id,
                    "status": "success",
                })
                payments_created += 1

            await collection(C.STUDENTS).update_one(
                {"_id": student["_id"]},
                {"$set": {"outstanding_amount": round(max(total - paid, 0), 2)}},
            )

    # ── An exam with marks ────────────────────────────────────────────────
    marks_created = 0
    if not await collection(C.EXAMS).find_one({"tenant_id": tenant_id}):
        exam_id = (await collection(C.EXAMS).insert_one({
            **stamp, "name": "Half Yearly 2026", "type": "midterm",
            "academic_year_id": year_id, "class_ids": list(class_ids.values()),
            "start_date": to_datetime(date(2026, 9, 1)),
            "end_date": to_datetime(date(2026, 9, 10)),
            "weightage_percent": 50, "status": "completed",
        })).inserted_id

        subjects = await collection(C.SUBJECTS).find({"tenant_id": tenant_id}).to_list(length=None)
        students = await collection(C.STUDENTS).find(
            {"tenant_id": tenant_id, "status": "active"}
        ).to_list(length=None)
        rows = []
        for student in students:
            for subject in subjects:
                if subject.get("class_id") != student.get("current_class_id"):
                    continue
                maximum = float(subject.get("max_marks", 100))
                obtained = round(random.triangular(maximum * 0.3, maximum, maximum * 0.72), 1)
                rows.append({
                    **stamp, "exam_id": exam_id, "student_id": student["_id"],
                    "subject_id": subject["_id"], "section_id": student.get("current_section_id"),
                    "class_id": student.get("current_class_id"),
                    "max_marks": maximum, "marks_obtained": obtained, "total_marks": obtained,
                    "percentage": round(obtained / maximum * 100, 2),
                    "is_pass": obtained >= float(subject.get("pass_marks", 33)),
                    "entered_at": utcnow(),
                })
        if rows:
            await collection(C.MARKS).insert_many(rows)
            marks_created = len(rows)

    # ── Announcements & events ────────────────────────────────────────────
    if not await collection(C.ANNOUNCEMENTS).find_one({"tenant_id": tenant_id}):
        await collection(C.ANNOUNCEMENTS).insert_many([
            {**stamp, "title": "Half Yearly results published",
             "summary": "Report cards are available in the parent portal.",
             "category": "exam", "priority": "high", "status": "published",
             "published_at": utcnow(), "audience": {"everyone": True}},
            {**stamp, "title": "Fee due date extended to 20 September",
             "summary": "Late fees will not apply until the 20th.",
             "category": "fee", "priority": "normal", "status": "published",
             "published_at": utcnow() - timedelta(days=2), "audience": {"everyone": True}},
            {**stamp, "title": "Annual Sports Day — 12 October",
             "summary": "Trials begin next week. Speak to your class teacher.",
             "category": "general", "priority": "normal", "status": "published",
             "published_at": utcnow() - timedelta(days=5), "audience": {"everyone": True}},
        ])

    if not await collection(C.EVENTS).find_one({"tenant_id": tenant_id}):
        await collection(C.EVENTS).insert_many([
            {**stamp, "title": "Parent–Teacher Meeting", "category": "ptm",
             "start_at": utcnow() + timedelta(days=6), "location": "Main Hall",
             "status": "scheduled", "audience": {"everyone": True}},
            {**stamp, "title": "Annual Sports Day", "category": "sports",
             "start_at": utcnow() + timedelta(days=28), "location": "School Ground",
             "status": "scheduled", "audience": {"everyone": True}},
            {**stamp, "title": "Gandhi Jayanti — Holiday", "category": "holiday",
             "start_at": to_datetime(date(2026, 10, 2)), "is_holiday": True,
             "status": "scheduled", "audience": {"everyone": True}},
        ])

    total_students = await collection(C.STUDENTS).count_documents(
        {"tenant_id": tenant_id, "is_deleted": {"$ne": True}}
    )
    return {
        "name": "Valley International School",
        "slug": slug,
        "logins": {
            "Owner (Super Admin)": f"head@valley.demo / {owner_password}",
        },
        "counts": (
            f"{total_students} students · {len(STAFF)} staff · "
            f"{attendance_written} attendance records · {invoices_created} invoices · "
            f"{payments_created} payments · {marks_created} marks"
        ),
    }
