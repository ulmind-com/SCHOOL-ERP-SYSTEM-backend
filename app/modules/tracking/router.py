"""Live transport tracking — driver app in, school and parents out."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.context import AuthContext
from app.core.deps import CurrentUser, TenantDep, require
from app.core.exceptions import Forbidden
from app.db.mongo import C, collection
from app.models.base import AppModel
from app.modules.tracking import service
from app.utils.audit import record

router = APIRouter(prefix="/tracking", tags=["Live Tracking"])

Viewer = Annotated[AuthContext, Depends(require("transport:read"))]
Driver = Annotated[AuthContext, Depends(require("transport:update"))]


class StartTripRequest(AppModel):
    vehicle_id: str
    route_id: str | None = None
    direction: str = "pickup"      # pickup | drop


class PositionRequest(AppModel):
    latitude: float
    longitude: float
    trip_id: str | None = None
    vehicle_id: str | None = None
    speed_kmh: float | None = None
    heading: float | None = None
    accuracy_m: float | None = None
    recorded_at: datetime | None = None


class PositionBatch(AppModel):
    """The driver app buffers while offline and flushes on reconnection."""

    positions: list[PositionRequest]


@router.get("/live", summary="Vehicles currently on the road")
async def live(auth: Viewer, tenant: TenantDep):
    return await service.live_vehicles(tenant)


@router.post("/trips", status_code=201, summary="Start a trip")
async def start_trip(
    payload: StartTripRequest, auth: Driver, tenant: TenantDep, request: Request
):
    result = await service.start_trip(
        tenant, auth,
        vehicle_id=payload.vehicle_id,
        route_id=payload.route_id,
        direction=payload.direction,
    )
    if not result.get("resumed"):
        await record(auth, "tracking.trip_started", entity_type="vehicle_trip",
                     entity_id=result.get("id"), request=request)
    return result


@router.post("/positions", summary="Report a position")
async def position(payload: PositionRequest, auth: Driver, tenant: TenantDep):
    return await service.record_position(tenant, auth, **payload.model_dump())


@router.post("/positions/batch", summary="Flush buffered positions")
async def positions_batch(payload: PositionBatch, auth: Driver, tenant: TenantDep):
    accepted, rejected = 0, 0
    last: dict | None = None
    for item in payload.positions[:500]:
        try:
            last = await service.record_position(tenant, auth, **item.model_dump())
            accepted += 1
        except Exception:
            # One bad fix in a buffered batch must not discard the rest.
            rejected += 1
    return {"accepted": accepted, "rejected": rejected, "last": last}


@router.post("/trips/{trip_id}/end", summary="End a trip")
async def end_trip(trip_id: str, auth: Driver, tenant: TenantDep, request: Request):
    result = await service.end_trip(tenant, auth, trip_id)
    await record(auth, "tracking.trip_ended", entity_type="vehicle_trip",
                 entity_id=trip_id, request=request)
    return result


@router.get("/trips", summary="Trip history")
async def trips(
    auth: Viewer,
    tenant: TenantDep,
    vehicle_id: str = "",
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    page: int = 1,
    page_size: Annotated[int, Query(le=100)] = 25,
):
    return await service.trip_history(
        tenant, vehicle_id=vehicle_id or None, days=days, page=page, page_size=page_size
    )


@router.get("/trips/{trip_id}/path", summary="A trip's recorded path")
async def path(trip_id: str, auth: Viewer, tenant: TenantDep):
    return await service.trip_path(tenant, trip_id)


@router.get("/routes/{route_id}/shape", summary="A route's stops, in order")
async def shape(route_id: str, auth: CurrentUser, tenant: TenantDep):
    """The part of a route that does not move. Open to anyone signed in — a
    parent needs the line their child's bus takes, not just the dot."""
    return await service.route_shape(tenant, route_id)


@router.get("/my-bus", summary="Where my child's bus is")
async def my_bus(auth: CurrentUser, tenant: TenantDep):
    """Families see only the vehicle their own children are allocated to."""
    student_ids: list = []
    if auth.student_id:
        student_ids = [auth.student_id]
    elif auth.guardian_id:
        guardian = await collection(C.GUARDIANS).find_one(
            {"_id": auth.guardian_id, "tenant_id": tenant.id}
        )
        student_ids = (guardian or {}).get("student_ids") or []
    else:
        raise Forbidden("This view is for students and parents")

    if not student_ids:
        return {"vehicles": [], "detail": "No transport allocated"}

    allocations = await collection(C.TRANSPORT_ALLOCATIONS).find({
        "tenant_id": tenant.id, "student_id": {"$in": student_ids}, "status": "active",
    }).to_list(length=20)
    route_ids = {a["route_id"] for a in allocations if a.get("route_id")}

    # Matched on the route's id. Matching on its *name* meant two routes called
    # "Route 2" put one family on the other's bus.
    live = await service.live_vehicles(tenant)
    wanted = {str(r) for r in route_ids}
    mine = [v for v in live if v.get("route_id") in wanted]

    students = {
        s["_id"]: " ".join(filter(None, [s.get("first_name"), s.get("last_name")]))
        for s in await collection(C.STUDENTS).find(
            {"_id": {"$in": student_ids}, "tenant_id": tenant.id},
            {"first_name": 1, "last_name": 1},
        ).to_list(length=None)
    }
    stops = {
        s["_id"]: s
        for s in await collection(C.TRANSPORT_STOPS).find(
            {"_id": {"$in": [a["stop_id"] for a in allocations if a.get("stop_id")]}},
            {"name": 1, "latitude": 1, "longitude": 1, "pickup_time": 1, "drop_time": 1},
        ).to_list(length=None)
    }

    riders = [
        {
            "student_id": str(a["student_id"]),
            "student_name": students.get(a["student_id"], ""),
            "route_id": str(a["route_id"]) if a.get("route_id") else None,
            "stop_name": (stops.get(a.get("stop_id")) or {}).get("name", ""),
            "stop_lat": (stops.get(a.get("stop_id")) or {}).get("latitude"),
            "stop_lng": (stops.get(a.get("stop_id")) or {}).get("longitude"),
            "pickup_time": (stops.get(a.get("stop_id")) or {}).get("pickup_time", ""),
            "drop_time": (stops.get(a.get("stop_id")) or {}).get("drop_time", ""),
            "direction": a.get("direction", "both"),
        }
        for a in allocations
    ]

    return {
        "vehicles": mine,
        "riders": riders,
        "detail": (
            "" if mine
            else "No bus is running on your route right now"
            if riders else "No transport allocated"
        ),
    }
