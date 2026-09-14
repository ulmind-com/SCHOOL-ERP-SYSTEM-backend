"""Biometric attendance.

Two audiences, two dialects:

* `/iclock/*` is spoken by the ZKTeco device itself — plain text in, plain text
  out, no JSON, no auth header. It is mounted at the application root because
  the firmware's server path is not configurable on many models.
* `/biometrics/*` is the ordinary authenticated API the school uses.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.config import settings
from app.core.context import AuthContext
from app.core.deps import TenantDep, require
from app.core.exceptions import Forbidden
from app.core.tenancy import build_context
from app.db.mongo import C, collection
from app.models.base import AppModel, Msg
from app.modules.biometrics import service
from app.utils.audit import record

# ── Device protocol (unauthenticated by design, gated by serial + key) ────
device_router = APIRouter(prefix="/iclock", tags=["Biometric Devices"],
                          include_in_schema=False)


def _plain(body: str = "OK") -> Response:
    # The firmware expects text/plain and ignores anything else.
    return Response(content=body, media_type="text/plain; charset=utf-8")


async def _authorise(serial: str, key: str | None) -> dict:
    """A device is trusted only if it is registered *and* presents the shared
    key. Without the key set, the endpoint refuses everything rather than
    accepting anonymous attendance."""
    if not settings.biometrics_enabled:
        raise Forbidden("Biometric ingestion is disabled (set BIOMETRIC_DEVICE_KEY)")
    if key != settings.biometric_device_key:
        raise Forbidden("Invalid device key")
    device = await service.device_by_serial(serial)
    if device is None:
        raise Forbidden("Device is not registered")
    return device


@device_router.get("/cdata", summary="Device handshake")
async def handshake(
    SN: str,
    request: Request,
    key: Annotated[str | None, Query()] = None,
    options: str = "",
    pushver: str = "",
):
    """First call after the device boots. The reply is a key=value block that
    tells the firmware how often to talk and what to send."""
    await _authorise(SN, key)
    await service.touch_device(SN, firmware=pushver or "")
    return _plain(
        "GET OPTION FROM: " + SN + "\n"
        "Stamp=0\n"
        "OpStamp=0\n"
        "ErrorDelay=30\n"
        "Delay=10\n"
        "TransTimes=00:00;12:00\n"
        "TransInterval=1\n"
        "TransFlag=1111000000\n"
        "Realtime=1\n"
        "Encrypt=0\n"
        "TimeZone=5.5\n"
    )


@device_router.post("/cdata", summary="Device pushes attendance")
async def push_attendance(
    SN: str,
    request: Request,
    key: Annotated[str | None, Query()] = None,
    table: str = "",
):
    device = await _authorise(SN, key)
    raw = (await request.body()).decode("utf-8", errors="replace")

    if table.upper() != "ATTLOG":
        # OPERLOG and friends: acknowledge so the device does not retry forever.
        await service.touch_device(SN)
        return _plain("OK")

    punches = service.parse_attlog(raw)
    result = await service.ingest_punches(device, punches)

    tenant_doc = await collection(C.TENANTS).find_one({"_id": device["tenant_id"]})
    if tenant_doc:
        await service.apply_punches(build_context(tenant_doc))

    # ADMS expects the count it should consider accepted.
    return _plain(f"OK: {result['received']}")


@device_router.get("/getrequest", summary="Device polls for commands")
async def get_request(SN: str, key: Annotated[str | None, Query()] = None):
    await _authorise(SN, key)
    await service.touch_device(SN)
    return _plain("OK")


@device_router.post("/devicecmd", summary="Device reports command results")
async def device_command(SN: str, key: Annotated[str | None, Query()] = None):
    await _authorise(SN, key)
    return _plain("OK")


@device_router.get("/ping", summary="Device heartbeat")
async def ping(SN: str, key: Annotated[str | None, Query()] = None):
    await _authorise(SN, key)
    await service.touch_device(SN)
    return _plain("OK")


# ── Institution API ───────────────────────────────────────────────────────
router = APIRouter(prefix="/biometrics", tags=["Biometric Attendance"])

Reader = Annotated[AuthContext, Depends(require("staff_attendance:read"))]
Manager = Annotated[AuthContext, Depends(require("staff_attendance:update"))]


class DeviceRequest(AppModel):
    serial_number: str
    name: str
    location: str = ""
    applies_to: str = "staff"
    timezone_offset_minutes: int = 330


class EnrolRequest(AppModel):
    person_type: str            # staff | student
    person_id: str
    biometric_id: str


@router.get("/devices", summary="Registered devices and their health")
async def devices(auth: Reader, tenant: TenantDep):
    return {
        "devices": await service.device_health(tenant),
        "ingestion_enabled": settings.biometrics_enabled,
        "push_url_hint": "/iclock/cdata?SN=<serial>&key=<BIOMETRIC_DEVICE_KEY>",
    }


@router.post("/devices", status_code=201, summary="Register a device")
async def add_device(
    payload: DeviceRequest, auth: Manager, tenant: TenantDep, request: Request
):
    device = await service.register_device(
        tenant,
        serial_number=payload.serial_number,
        name=payload.name,
        location=payload.location,
        applies_to=payload.applies_to,
        timezone_offset_minutes=payload.timezone_offset_minutes,
    )
    await record(auth, "biometrics.device_registered", entity_type="biometric_device",
                 entity_label=payload.serial_number, request=request)
    return device


@router.delete("/devices/{serial_number}", response_model=Msg, summary="Remove a device")
async def remove_device(
    serial_number: str, auth: Manager, tenant: TenantDep, request: Request
):
    from app.models.base import utcnow

    await collection(C.BIOMETRIC_DEVICES).update_one(
        {"serial_number": serial_number.upper(), "tenant_id": tenant.id},
        {"$set": {"is_deleted": True, "updated_at": utcnow()}},
    )
    await record(auth, "biometrics.device_removed", entity_type="biometric_device",
                 entity_label=serial_number, request=request)
    return Msg(detail="Device removed")


@router.get("/punches", summary="Raw punch log")
async def punches(
    auth: Reader,
    tenant: TenantDep,
    page: int = 1,
    page_size: Annotated[int, Query(le=200)] = 50,
    processed: bool | None = None,
):
    return await service.recent_punches(
        tenant, page=page, page_size=page_size, processed=processed
    )


@router.get("/unmatched", summary="Enrolment ids nobody is linked to")
async def unmatched(auth: Reader, tenant: TenantDep):
    return await service.unmatched_ids(tenant)


@router.post("/enrol", summary="Link an enrolment id to a person")
async def enrol(payload: EnrolRequest, auth: Manager, tenant: TenantDep, request: Request):
    result = await service.enrol(
        tenant, person_type=payload.person_type, person_id=payload.person_id,
        biometric_id=payload.biometric_id,
    )
    await record(auth, "biometrics.enrolled", entity_type=payload.person_type,
                 entity_id=payload.person_id,
                 changes={"biometric_id": payload.biometric_id}, request=request)
    return result


@router.post("/sync", summary="Apply pending punches to attendance")
async def sync(
    auth: Manager, tenant: TenantDep, request: Request, on: datetime | None = None
):
    """Normally automatic when a device pushes; exposed for corrections and for
    devices that batch overnight."""
    result = await service.apply_punches(tenant, on=on)
    await record(auth, "biometrics.sync", entity_type="attendance",
                 changes=result, request=request)
    return result
