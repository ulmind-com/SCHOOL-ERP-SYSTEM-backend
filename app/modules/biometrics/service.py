"""ZKTeco / ADMS biometric attendance.

ZKTeco devices in "push" mode talk plain HTTP to a fixed set of paths and
expect plain-text replies — not JSON, not an envelope, just `OK` or a
line-oriented body. The endpoints in `router.py` implement that dialect; this
module turns what arrives into attendance an institution can read.

Two decisions worth stating:

* A punch is stored raw *and* separately applied. Devices re-send on
  reconnection, and a school's clock skew is real, so the raw log is the
  evidence and the attendance record is the interpretation. Re-applying is
  idempotent.
* The first punch of a day is the in-time and the last is the out-time. Most
  schools have staff and students pass the reader more than twice.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any

from bson import ObjectId

from app.core.context import TenantContext
from app.core.exceptions import NotFound, ValidationError
from app.db.mongo import C, collection
from app.models.base import serialize_doc, utcnow

log = logging.getLogger("scholarly.biometrics")

# ZKTeco punch state codes. 0/4 are the common check-in variants, 1/5 check-out.
CHECK_IN_STATES = {"0", "4"}
CHECK_OUT_STATES = {"1", "5"}


async def register_device(
    tenant: TenantContext,
    *,
    serial_number: str,
    name: str,
    location: str = "",
    applies_to: str = "staff",
    timezone_offset_minutes: int = 330,
) -> dict[str, Any]:
    serial_number = serial_number.strip().upper()
    if not serial_number:
        raise ValidationError("The device serial number is required")

    clash = await collection(C.BIOMETRIC_DEVICES).find_one({"serial_number": serial_number})
    if clash and clash.get("tenant_id") != tenant.id:
        raise ValidationError("That serial number is already registered elsewhere")

    doc = await collection(C.BIOMETRIC_DEVICES).find_one_and_update(
        {"serial_number": serial_number},
        {
            "$set": {
                "tenant_id": tenant.id,
                "name": name,
                "location": location,
                "applies_to": applies_to,     # staff | students | both
                "timezone_offset_minutes": timezone_offset_minutes,
                "updated_at": utcnow(),
            },
            "$setOnInsert": {
                "status": "pending",
                "last_seen_at": None,
                "punch_count": 0,
                "created_at": utcnow(),
                "is_deleted": False,
            },
        },
        upsert=True,
        return_document=True,
    )
    return serialize_doc(doc)  # type: ignore[return-value]


async def device_by_serial(serial_number: str) -> dict[str, Any] | None:
    return await collection(C.BIOMETRIC_DEVICES).find_one(
        {"serial_number": serial_number.strip().upper(), "is_deleted": {"$ne": True}}
    )


async def touch_device(serial_number: str, **fields: Any) -> None:
    await collection(C.BIOMETRIC_DEVICES).update_one(
        {"serial_number": serial_number.strip().upper()},
        {"$set": {"last_seen_at": utcnow(), "status": "online", "updated_at": utcnow(), **fields}},
    )


def parse_attlog(body: str) -> list[dict[str, Any]]:
    """Parse an ADMS ATTLOG payload.

    The documented shape is tab-separated:

        <enroll id>\t<YYYY-MM-DD HH:MM:SS>\t<status>\t<verify mode>\t…

    but firmware in the field also emits space-separated lines, where the
    timestamp arrives as *two* tokens. The timestamp is therefore located
    first, and everything after it is read relative to where it ended — reading
    the status from a fixed index silently mislabels every punch on one of the
    two dialects.
    """
    punches: list[dict[str, Any]] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p for p in (line.split("\t") if "\t" in line else line.split()) if p != ""]
        if len(parts) < 2:
            continue

        punched_at: datetime | None = None
        after = 2
        # One token holding "date time"?
        try:
            punched_at = datetime.strptime(parts[1].strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # Two tokens: date, then time.
            if len(parts) >= 3:
                try:
                    punched_at = datetime.strptime(
                        f"{parts[1].strip()} {parts[2].strip()}", "%Y-%m-%d %H:%M:%S"
                    )
                    after = 3
                except ValueError:
                    punched_at = None

        if punched_at is None:
            log.warning("Unparseable punch line: %r", line[:120])
            continue

        rest = parts[after:]
        punches.append({
            "biometric_id": parts[0].strip(),
            "punched_at": punched_at,
            "state": (rest[0] if rest else "0").strip(),
            "verify_mode": (rest[1] if len(rest) > 1 else "").strip(),
            "raw": line[:300],
        })
    return punches


async def ingest_punches(
    device: dict[str, Any], punches: list[dict[str, Any]]
) -> dict[str, Any]:
    """Store raw punches, skipping ones already seen."""
    if not punches:
        return {"received": 0, "stored": 0}

    tenant_id = device["tenant_id"]
    offset = timedelta(minutes=int(device.get("timezone_offset_minutes") or 0))
    serial = device["serial_number"]

    stored = 0
    for punch in punches:
        # Two timestamps, deliberately.
        #
        # `punched_at` is a real instant in UTC, so punches from devices in
        # different timezones sort and query together.
        #
        # `local_time` is the wall clock the device showed, kept as a *string*.
        # BSON has no naive datetime — store one and it comes back tagged UTC,
        # and every client helpfully shifts it again. A wall clock is not an
        # instant, so it is not stored as one.
        local_punched_at = punch["punched_at"]
        punched_at = local_punched_at.replace(tzinfo=UTC) - offset
        result = await collection(C.BIOMETRIC_PUNCHES).update_one(
            {
                "tenant_id": tenant_id,
                "device_serial": serial,
                "biometric_id": punch["biometric_id"],
                "punched_at": punched_at,
            },
            {
                "$setOnInsert": {
                    "tenant_id": tenant_id,
                    "device_serial": serial,
                    "biometric_id": punch["biometric_id"],
                    "punched_at": punched_at,
                    "local_time": local_punched_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "state": punch["state"],
                    "verify_mode": punch["verify_mode"],
                    "raw": punch["raw"],
                    "processed": False,
                    "created_at": utcnow(),
                    "is_deleted": False,
                }
            },
            upsert=True,
        )
        if result.upserted_id is not None:
            stored += 1

    await collection(C.BIOMETRIC_DEVICES).update_one(
        {"serial_number": serial},
        {"$inc": {"punch_count": stored}, "$set": {"last_seen_at": utcnow()}},
    )
    return {"received": len(punches), "stored": stored}


async def apply_punches(tenant: TenantContext, *, on: datetime | None = None) -> dict[str, Any]:
    """Turn raw punches into staff and student attendance.

    Safe to re-run: attendance rows are upserted, and a punch is only marked
    processed once it has been applied.
    """
    query: dict[str, Any] = {"tenant_id": tenant.id, "processed": False,
                             "is_deleted": {"$ne": True}}
    if on is not None:
        day = on.replace(hour=0, minute=0, second=0, microsecond=0)
        query["punched_at"] = {"$gte": day, "$lt": day + timedelta(days=1)}

    punches = await collection(C.BIOMETRIC_PUNCHES).find(query).sort(
        [("punched_at", 1)]
    ).to_list(length=20000)
    if not punches:
        return {"punches": 0, "staff_days": 0, "student_days": 0, "unmatched": 0}

    # Biometric id → person. Institutions enrol staff and students separately.
    staff = {
        str(s.get("biometric_id")): s
        for s in await collection(C.STAFF).find(
            {"tenant_id": tenant.id, "biometric_id": {"$nin": [None, ""]}},
            {"biometric_id": 1},
        ).to_list(length=None)
    }
    students = {
        str(s.get("biometric_id")): s
        for s in await collection(C.STUDENTS).find(
            {"tenant_id": tenant.id, "biometric_id": {"$nin": [None, ""]}},
            {"biometric_id": 1, "current_section_id": 1, "current_class_id": 1,
             "academic_year_id": 1},
        ).to_list(length=None)
    }

    settings = (tenant.settings or {}).get("biometrics", {})
    late_after = _parse_clock(settings.get("late_after", "09:15"))
    half_day_after = _parse_clock(settings.get("half_day_after", "11:30"))

    # Collapse to one row per person per day.
    grouped: dict[tuple[str, str], list[datetime]] = {}
    matched_ids: list[ObjectId] = []
    unmatched = 0

    for punch in punches:
        biometric_id = str(punch.get("biometric_id"))
        if biometric_id not in staff and biometric_id not in students:
            unmatched += 1
            continue
        # Group and display by local wall clock: a 16:10 punch belongs to that
        # school day, even where UTC has already rolled past midnight.
        raw_local = punch.get("local_time")
        if raw_local:
            when = datetime.fromisoformat(raw_local).replace(tzinfo=UTC)
        else:
            when = punch["punched_at"]
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
        grouped.setdefault((biometric_id, when.date().isoformat()), []).append(when)
        matched_ids.append(punch["_id"])

    staff_days = student_days = 0
    for (biometric_id, day_iso), stamps in grouped.items():
        stamps.sort()
        first, last = stamps[0], stamps[-1]
        day = datetime.fromisoformat(day_iso).replace(tzinfo=UTC)
        in_time = first.strftime("%H:%M")
        out_time = last.strftime("%H:%M") if last != first else ""
        clock = first.time()

        if clock > half_day_after:
            status = "half_day"
        elif clock > late_after:
            status = "late"
        else:
            status = "present"
        minutes_late = max(
            int((datetime.combine(first.date(), clock) -
                 datetime.combine(first.date(), late_after)).total_seconds() // 60), 0
        )

        if biometric_id in staff:
            await collection(C.STAFF_ATTENDANCE).update_one(
                {"tenant_id": tenant.id, "staff_id": staff[biometric_id]["_id"], "date": day},
                {"$set": {
                    "status": status, "in_time": in_time, "out_time": out_time,
                    "minutes_late": minutes_late, "source": "biometric",
                    "worked_hours": round((last - first).total_seconds() / 3600, 2),
                    "updated_at": utcnow(),
                },
                 "$setOnInsert": {"created_at": utcnow(), "is_deleted": False}},
                upsert=True,
            )
            staff_days += 1
        else:
            student = students[biometric_id]
            await collection(C.ATTENDANCE).update_one(
                {"tenant_id": tenant.id, "student_id": student["_id"], "date": day,
                 "session_key": "day"},
                {"$set": {
                    "status": status, "in_time": in_time,
                    "minutes_late": minutes_late,
                    "section_id": student.get("current_section_id"),
                    "class_id": student.get("current_class_id"),
                    "academic_year_id": student.get("academic_year_id"),
                    "remark": "Biometric", "updated_at": utcnow(),
                },
                 "$setOnInsert": {"created_at": utcnow(), "is_deleted": False}},
                upsert=True,
            )
            student_days += 1

    if matched_ids:
        await collection(C.BIOMETRIC_PUNCHES).update_many(
            {"_id": {"$in": matched_ids}},
            {"$set": {"processed": True, "processed_at": utcnow()}},
        )

    return {
        "punches": len(punches),
        "staff_days": staff_days,
        "student_days": student_days,
        "unmatched": unmatched,
        "detail": (
            f"{staff_days} staff and {student_days} student attendance day(s) updated"
            + (f"; {unmatched} punch(es) from unenrolled ids" if unmatched else "")
        ),
    }


def _parse_clock(value: str) -> time:
    try:
        hours, minutes = str(value).split(":")[:2]
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return time(9, 15)


async def device_health(tenant: TenantContext) -> list[dict[str, Any]]:
    devices = await collection(C.BIOMETRIC_DEVICES).find(
        {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    ).sort([("name", 1)]).to_list(length=200)

    now = utcnow()
    out = []
    for device in devices:
        last_seen = device.get("last_seen_at")
        minutes = (
            int((now - last_seen.replace(tzinfo=UTC)).total_seconds() // 60)
            if last_seen else None
        )
        row = serialize_doc(device) or {}
        row["minutes_since_seen"] = minutes
        # A device that has not checked in for ten minutes is not pushing.
        row["status"] = (
            "never" if minutes is None else "online" if minutes <= 10 else "offline"
        )
        out.append(row)
    return out


async def unmatched_ids(tenant: TenantContext) -> list[dict[str, Any]]:
    """Biometric ids the devices are sending that nobody is enrolled against —
    the first thing to check when attendance "isn't working"."""
    enrolled_staff = await collection(C.STAFF).distinct(
        "biometric_id", {"tenant_id": tenant.id, "biometric_id": {"$nin": [None, ""]}}
    )
    enrolled_students = await collection(C.STUDENTS).distinct(
        "biometric_id", {"tenant_id": tenant.id, "biometric_id": {"$nin": [None, ""]}}
    )
    known = {str(v) for v in [*enrolled_staff, *enrolled_students]}

    rows = await collection(C.BIOMETRIC_PUNCHES).aggregate([
        {"$match": {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}},
        {"$group": {"_id": "$biometric_id", "punches": {"$sum": 1},
                    "last_seen": {"$max": "$punched_at"},
                    "devices": {"$addToSet": "$device_serial"}}},
        {"$sort": {"punches": -1}}, {"$limit": 100},
    ]).to_list(length=100)

    return [
        {
            "biometric_id": row["_id"],
            "punches": row["punches"],
            "last_seen": row["last_seen"].isoformat() if row.get("last_seen") else None,
            "devices": row["devices"],
        }
        for row in rows
        if str(row["_id"]) not in known
    ]


async def enrol(
    tenant: TenantContext, *, person_type: str, person_id: str, biometric_id: str
) -> dict[str, Any]:
    """Link a device's enrolment number to a person."""
    biometric_id = biometric_id.strip()
    if not biometric_id:
        raise ValidationError("Enter the enrolment number shown on the device")

    target = C.STAFF if person_type == "staff" else C.STUDENTS
    clash = await collection(target).find_one({
        "tenant_id": tenant.id, "biometric_id": biometric_id,
        "_id": {"$ne": ObjectId(person_id)}, "is_deleted": {"$ne": True},
    })
    if clash:
        raise ValidationError("That enrolment number is already linked to someone else")

    result = await collection(target).update_one(
        {"_id": ObjectId(person_id), "tenant_id": tenant.id},
        {"$set": {"biometric_id": biometric_id, "updated_at": utcnow()}},
    )
    if result.matched_count == 0:
        raise NotFound("Person not found")
    return {"person_id": person_id, "biometric_id": biometric_id,
            "detail": "Enrolment linked"}


async def recent_punches(
    tenant: TenantContext, *, page: int = 1, page_size: int = 50, processed: bool | None = None
) -> dict[str, Any]:
    query: dict[str, Any] = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    if processed is not None:
        query["processed"] = processed

    punches = collection(C.BIOMETRIC_PUNCHES)
    total = await punches.count_documents(query)
    docs = await punches.find(query).sort([("punched_at", -1)]).skip(
        (page - 1) * page_size
    ).limit(page_size).to_list(length=page_size)

    ids = {str(d.get("biometric_id")) for d in docs}
    staff = {
        str(s.get("biometric_id")): f"{s.get('first_name','')} {s.get('last_name','')}".strip()
        for s in await collection(C.STAFF).find(
            {"tenant_id": tenant.id, "biometric_id": {"$in": list(ids)}},
            {"biometric_id": 1, "first_name": 1, "last_name": 1},
        ).to_list(length=None)
    }
    students = {
        str(s.get("biometric_id")): f"{s.get('first_name','')} {s.get('last_name','')}".strip()
        for s in await collection(C.STUDENTS).find(
            {"tenant_id": tenant.id, "biometric_id": {"$in": list(ids)}},
            {"biometric_id": 1, "first_name": 1, "last_name": 1},
        ).to_list(length=None)
    }

    items = []
    for doc in docs:
        row = serialize_doc(doc) or {}
        key = str(doc.get("biometric_id"))
        row["person"] = staff.get(key) or students.get(key) or ""
        row["person_type"] = "staff" if key in staff else "student" if key in students else ""
        items.append(row)

    total_pages = max(1, -(-total // page_size))
    return {"items": items, "meta": {"page": page, "page_size": page_size, "total": total,
                                     "total_pages": total_pages, "has_next": page < total_pages,
                                     "has_prev": page > 1}}
