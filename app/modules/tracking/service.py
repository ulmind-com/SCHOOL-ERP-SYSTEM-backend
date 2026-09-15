"""Live vehicle tracking.

The driver app posts a position every few seconds. Three things follow from
that being the only input:

* Positions are append-only and expire after 30 days (a TTL index), because a
  term's worth of 5-second pings is millions of rows nobody reads.
* A *trip* is the unit a school actually cares about ("did the morning run
  happen, and when did it reach the school"), so positions are attached to one.
* Speed and distance are derived here, not trusted from the device — phone GPS
  reports nonsense speeds when the signal drops.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Any

from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.core.exceptions import NotFound, ValidationError
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models.base import serialize_doc, utcnow

EARTH_RADIUS_KM = 6371.0
#: Faster than this between two pings is not a bus.
MAX_PLAUSIBLE_KMH = 140.0
#: Below this gap, an implausible speed means the fix jumped — the vehicle
#: cannot have moved. Above it, the app was simply offline and the distance is
#: real even though the implied speed is not.
GLITCH_WINDOW_SECONDS = 60
#: How close a vehicle must be to count as "at" a stop.
STOP_RADIUS_METRES = 120.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = (
        sin(d_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


async def start_trip(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    vehicle_id: str,
    route_id: str | None = None,
    direction: str = "pickup",
) -> dict[str, Any]:
    trips = Repository(C.VEHICLE_TRIPS, tenant.id, actor_id=auth.user_id)

    # A vehicle can only be on one trip; an app crash must not strand it.
    open_trip = await trips.find_one({"vehicle_id": ObjectId(vehicle_id), "status": "active"})
    if open_trip:
        return {**(serialize_doc(open_trip) or {}), "resumed": True,
                "detail": "Rejoined the trip already in progress"}

    vehicle = await collection(C.TRANSPORT_VEHICLES).find_one(
        {"_id": ObjectId(vehicle_id), "tenant_id": tenant.id}
    )
    if vehicle is None:
        raise NotFound("Vehicle not found")

    trip = await trips.create({
        "vehicle_id": ObjectId(vehicle_id),
        "vehicle_number": vehicle.get("registration_number", ""),
        "route_id": ObjectId(route_id) if route_id and ObjectId.is_valid(route_id) else None,
        "direction": direction,
        "driver_user_id": auth.user_id,
        "driver_name": auth.full_name,
        "status": "active",
        "started_at": utcnow(),
        "ended_at": None,
        "distance_km": 0.0,
        "max_speed_kmh": 0.0,
        "point_count": 0,
        "stops_reached": [],
    })
    return {**(serialize_doc(trip) or {}), "resumed": False, "detail": "Trip started"}


async def record_position(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    latitude: float,
    longitude: float,
    trip_id: str | None = None,
    vehicle_id: str | None = None,
    speed_kmh: float | None = None,
    heading: float | None = None,
    accuracy_m: float | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        raise ValidationError("That is not a valid coordinate")

    trips = Repository(C.VEHICLE_TRIPS, tenant.id, actor_id=auth.user_id)
    trip = None
    if trip_id and ObjectId.is_valid(trip_id):
        trip = await trips.find_one({"_id": ObjectId(trip_id)})
    elif vehicle_id and ObjectId.is_valid(vehicle_id):
        trip = await trips.find_one({"vehicle_id": ObjectId(vehicle_id), "status": "active"})
    if trip is None:
        raise NotFound("No active trip — start one before sending positions")

    when = recorded_at or utcnow()
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

    previous = await collection(C.VEHICLE_POSITIONS).find_one(
        {"tenant_id": tenant.id, "trip_id": trip["_id"]}, sort=[("recorded_at", -1)]
    )

    leg_km = 0.0
    derived_kmh = speed_kmh or 0.0
    if previous:
        leg_km = haversine_km(
            previous["latitude"], previous["longitude"], latitude, longitude
        )
        seconds = (when - previous["recorded_at"].replace(tzinfo=UTC)).total_seconds()
        if seconds > 0:
            derived_kmh = leg_km / (seconds / 3600)

            if derived_kmh > MAX_PLAUSIBLE_KMH:
                if seconds < GLITCH_WINDOW_SECONDS:
                    # Teleport within a minute: the fix is wrong, not the bus.
                    # Keep the point — it is the best guess at where the vehicle
                    # is — but do not let it inflate the trip distance.
                    leg_km = 0.0
                    derived_kmh = float(previous.get("speed_kmh") or 0.0)
                else:
                    # The app was offline and flushed on reconnection. The
                    # distance really was covered; only the implied speed is
                    # meaningless, so report the vehicle's last known speed.
                    derived_kmh = float(previous.get("speed_kmh") or 0.0)

    await collection(C.VEHICLE_POSITIONS).insert_one({
        "tenant_id": tenant.id,
        "trip_id": trip["_id"],
        "vehicle_id": trip["vehicle_id"],
        "latitude": latitude,
        "longitude": longitude,
        "speed_kmh": round(derived_kmh, 2),
        "heading": heading,
        "accuracy_m": accuracy_m,
        "recorded_at": when,
        "created_at": utcnow(),
    })

    reached = await _check_stops(tenant, trip, latitude, longitude)

    await collection(C.VEHICLE_TRIPS).update_one(
        {"_id": trip["_id"]},
        {
            "$inc": {"distance_km": round(leg_km, 4), "point_count": 1},
            "$max": {"max_speed_kmh": round(derived_kmh, 2)},
            "$set": {
                "last_latitude": latitude,
                "last_longitude": longitude,
                "last_speed_kmh": round(derived_kmh, 2),
                "last_position_at": when,
                "updated_at": utcnow(),
            },
            **({"$addToSet": {"stops_reached": reached}} if reached else {}),
        },
    )

    return {
        "trip_id": str(trip["_id"]),
        "speed_kmh": round(derived_kmh, 2),
        "leg_km": round(leg_km, 4),
        "stop_reached": reached,
    }


async def _check_stops(
    tenant: TenantContext, trip: dict[str, Any], latitude: float, longitude: float
) -> dict[str, Any] | None:
    """Notify a stop's families when the bus actually arrives there."""
    if not trip.get("route_id"):
        return None

    already = {s.get("stop_id") for s in (trip.get("stops_reached") or [])}
    stops = await collection(C.TRANSPORT_STOPS).find({
        "tenant_id": tenant.id, "route_id": trip["route_id"],
        "latitude": {"$ne": None}, "is_deleted": {"$ne": True},
    }).to_list(length=100)

    for stop in stops:
        if str(stop["_id"]) in already:
            continue
        distance_m = haversine_km(
            stop["latitude"], stop["longitude"], latitude, longitude
        ) * 1000
        if distance_m <= STOP_RADIUS_METRES:
            await _notify_stop(tenant, trip, stop)
            return {
                "stop_id": str(stop["_id"]),
                "name": stop.get("name", ""),
                "reached_at": utcnow().isoformat(),
            }
    return None


async def _notify_stop(
    tenant: TenantContext, trip: dict[str, Any], stop: dict[str, Any]
) -> None:
    from app.modules.communication.notify import notify_users

    allocations = await collection(C.TRANSPORT_ALLOCATIONS).find({
        "tenant_id": tenant.id, "stop_id": stop["_id"], "status": "active",
    }).to_list(length=500)
    if not allocations:
        return

    student_ids = [a["student_id"] for a in allocations]
    students = await collection(C.STUDENTS).find(
        {"_id": {"$in": student_ids}}, {"guardian_ids": 1}
    ).to_list(length=None)
    guardian_ids = [g for s in students for g in (s.get("guardian_ids") or [])]

    user_ids = await collection(C.USERS).distinct("_id", {
        "tenant_id": tenant.id, "is_active": True,
        "$or": [{"student_id": {"$in": student_ids}},
                {"guardian_id": {"$in": guardian_ids}}],
    })
    if not user_ids:
        return

    await notify_users(
        tenant, user_ids,
        title=f"Bus {trip.get('vehicle_number', '')} has reached {stop.get('name', '')}",
        body="Please be at the stop.",
        category="transport",
        type="info",
        link="/facilities/transport",
    )


async def end_trip(tenant: TenantContext, auth: AuthContext, trip_id: str) -> dict[str, Any]:
    trips = Repository(C.VEHICLE_TRIPS, tenant.id, actor_id=auth.user_id)
    trip = await trips.get_or_404(trip_id, label="Trip")
    if trip.get("status") != "active":
        return {**(serialize_doc(trip) or {}), "detail": "Trip was already closed"}

    started = trip["started_at"].replace(tzinfo=UTC)
    minutes = int((utcnow() - started).total_seconds() // 60)
    updated = await trips.update(trip_id, {
        "status": "completed",
        "ended_at": utcnow(),
        "duration_minutes": minutes,
        "distance_km": round(float(trip.get("distance_km") or 0), 2),
    })
    return {**(serialize_doc(updated) or {}), "detail": f"Trip closed after {minutes} minutes"}


async def live_vehicles(tenant: TenantContext) -> list[dict[str, Any]]:
    """Everything currently on the road, with staleness made explicit — a
    vehicle whose driver's phone died must not look like it is parked."""
    trips = await collection(C.VEHICLE_TRIPS).find(
        {"tenant_id": tenant.id, "status": "active", "is_deleted": {"$ne": True}}
    ).to_list(length=200)

    routes = {
        r["_id"]: r
        for r in await collection(C.TRANSPORT_ROUTES).find(
            {"tenant_id": tenant.id}
        ).to_list(length=None)
    }
    # The map draws a bus differently from a car, and a marker that matches what
    # is actually on the road is the difference between a map you trust and a
    # map you squint at.
    vehicles = {
        v["_id"]: v
        for v in await collection(C.TRANSPORT_VEHICLES).find(
            {"_id": {"$in": [t["vehicle_id"] for t in trips if t.get("vehicle_id")]}},
            {"type": 1, "model": 1, "capacity": 1, "driver_phone": 1},
        ).to_list(length=None)
    }

    now = utcnow()
    out = []
    for trip in trips:
        last = trip.get("last_position_at")
        stale_seconds = (
            int((now - last.replace(tzinfo=UTC)).total_seconds()) if last else None
        )
        route = routes.get(trip.get("route_id"), {})
        vehicle = vehicles.get(trip.get("vehicle_id"), {})
        out.append({
            "trip_id": str(trip["_id"]),
            "vehicle_id": str(trip["vehicle_id"]),
            "vehicle_number": trip.get("vehicle_number", ""),
            "vehicle_type": vehicle.get("type", "bus"),
            "vehicle_model": vehicle.get("model", ""),
            "capacity": vehicle.get("capacity", 0),
            "driver_phone": vehicle.get("driver_phone", ""),
            "route_id": str(trip["route_id"]) if trip.get("route_id") else None,
            "route": route.get("name", ""),
            "direction": trip.get("direction", ""),
            "driver": trip.get("driver_name", ""),
            "latitude": trip.get("last_latitude"),
            "longitude": trip.get("last_longitude"),
            "speed_kmh": trip.get("last_speed_kmh", 0),
            "distance_km": round(float(trip.get("distance_km") or 0), 2),
            "started_at": trip["started_at"].isoformat() if trip.get("started_at") else None,
            "last_position_at": last.isoformat() if last else None,
            "seconds_since_update": stale_seconds,
            "signal": (
                "no-fix" if stale_seconds is None
                else "live" if stale_seconds <= 60
                else "delayed" if stale_seconds <= 600
                else "lost"
            ),
            "stops_reached": trip.get("stops_reached") or [],
        })
    return out


async def route_shape(tenant: TenantContext, route_id: str) -> dict[str, Any]:
    """A route as the map needs it: its stops, in order, with coordinates.

    Separate from the live position because it is the part that does not move.
    Drawn before any bus is out, so a parent opening the screen at seven in the
    morning sees the line their child's bus will take rather than a blank tile.
    """
    route = await collection(C.TRANSPORT_ROUTES).find_one(
        {"_id": ObjectId(route_id), "tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    )
    if route is None:
        raise NotFound("Route not found")

    stops = await collection(C.TRANSPORT_STOPS).find({
        "tenant_id": tenant.id, "route_id": route["_id"], "is_deleted": {"$ne": True},
    }).sort([("order", 1)]).to_list(length=200)

    return {
        "id": str(route["_id"]),
        "name": route.get("name", ""),
        "code": route.get("code", ""),
        "distance_km": route.get("distance_km", 0),
        "stops": [
            {
                "id": str(stop["_id"]),
                "name": stop.get("name", ""),
                "order": stop.get("order", 0),
                "lat": stop.get("latitude"),
                "lng": stop.get("longitude"),
                "pickup_time": stop.get("pickup_time", ""),
                "drop_time": stop.get("drop_time", ""),
                "landmark": stop.get("landmark", ""),
            }
            for stop in stops
        ],
    }


async def trip_path(tenant: TenantContext, trip_id: str) -> dict[str, Any]:
    trip = await collection(C.VEHICLE_TRIPS).find_one(
        {"_id": ObjectId(trip_id), "tenant_id": tenant.id}
    )
    if trip is None:
        raise NotFound("Trip not found")

    positions = await collection(C.VEHICLE_POSITIONS).find(
        {"tenant_id": tenant.id, "trip_id": trip["_id"]},
        {"latitude": 1, "longitude": 1, "speed_kmh": 1, "recorded_at": 1},
    ).sort([("recorded_at", 1)]).to_list(length=5000)

    return {
        "trip": serialize_doc(trip),
        "path": [
            {
                "lat": p["latitude"],
                "lng": p["longitude"],
                "speed": p.get("speed_kmh", 0),
                "at": p["recorded_at"].isoformat(),
            }
            for p in positions
        ],
    }


async def trip_history(
    tenant: TenantContext, *, vehicle_id: str | None = None, days: int = 7,
    page: int = 1, page_size: int = 25,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "tenant_id": tenant.id, "is_deleted": {"$ne": True},
        "started_at": {"$gte": utcnow() - timedelta(days=days)},
    }
    if vehicle_id and ObjectId.is_valid(vehicle_id):
        query["vehicle_id"] = ObjectId(vehicle_id)

    trips = collection(C.VEHICLE_TRIPS)
    total = await trips.count_documents(query)
    docs = await trips.find(query).sort([("started_at", -1)]).skip(
        (page - 1) * page_size
    ).limit(page_size).to_list(length=page_size)

    total_pages = max(1, -(-total // page_size))
    return {
        "items": [serialize_doc(d) for d in docs],
        "meta": {"page": page, "page_size": page_size, "total": total,
                 "total_pages": total_pages, "has_next": page < total_pages,
                 "has_prev": page > 1},
    }
