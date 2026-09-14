"""Library, transport, hostel, inventory and gate management."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, computed_field

from app.models.base import FileRef, PyObjectId, TenantDocument


# ── Library ───────────────────────────────────────────────────────────────
class LibraryItem(TenantDocument):
    accession_number: str
    title: str
    author: str = ""
    publisher: str = ""
    isbn: str = ""
    edition: str = ""
    category: str = "book"          # book | journal | magazine | thesis | media | ebook
    subject_area: str = ""
    language: str = "English"
    shelf: str = ""
    price: float = 0
    published_year: str = ""
    total_copies: int = 1
    available_copies: int = 1
    cover: FileRef | None = None
    status: str = "available"       # available | issued | reserved | lost | damaged | archived
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class LibraryLoan(TenantDocument):
    item_id: PyObjectId
    item_title: str = ""
    borrower_type: str = "student"  # student | staff
    borrower_id: PyObjectId
    borrower_name: str = ""
    issued_on: datetime
    due_date: datetime
    returned_on: datetime | None = None
    renewed_count: int = 0
    status: str = "issued"          # issued | returned | overdue | lost
    fine_amount: float = 0
    fine_paid: bool = False
    issued_by: PyObjectId | None = None
    received_by: PyObjectId | None = None
    remarks: str = ""


# ── Transport ─────────────────────────────────────────────────────────────
class Vehicle(TenantDocument):
    registration_number: str
    model: str = ""
    type: str = "bus"               # bus | van | car
    capacity: int = 40
    driver_name: str = ""
    driver_phone: str = ""
    driver_licence: str = ""
    attendant_name: str = ""
    attendant_phone: str = ""
    insurance_expiry: datetime | None = None
    fitness_expiry: datetime | None = None
    permit_expiry: datetime | None = None
    gps_device_id: str = ""
    status: str = "active"          # active | maintenance | retired
    photo: FileRef | None = None


class TransportStop(TenantDocument):
    route_id: PyObjectId
    name: str
    order: int = 1
    pickup_time: str = ""
    drop_time: str = ""
    latitude: float | None = None
    longitude: float | None = None
    landmark: str = ""
    monthly_fare: float = 0


class TransportRoute(TenantDocument):
    code: str
    name: str
    vehicle_id: PyObjectId | None = None
    start_point: str = ""
    end_point: str = ""
    distance_km: float = 0
    monthly_fare: float = 0
    stop_count: int = 0
    allocated_count: int = 0
    is_active: bool = True


class TransportAllocation(TenantDocument):
    student_id: PyObjectId
    route_id: PyObjectId
    stop_id: PyObjectId | None = None
    academic_year_id: PyObjectId | None = None
    direction: str = "both"         # pickup | drop | both
    monthly_fare: float = 0
    start_date: datetime | None = None
    end_date: datetime | None = None
    status: str = "active"


# ── Hostel ────────────────────────────────────────────────────────────────
class Hostel(TenantDocument):
    name: str
    type: str = "boys"              # boys | girls | mixed
    warden_staff_id: PyObjectId | None = None
    address: str = ""
    total_rooms: int = 0
    total_capacity: int = 0
    occupied: int = 0
    monthly_fee: float = 0
    facilities: list[str] = Field(default_factory=list)
    is_active: bool = True


class HostelRoom(TenantDocument):
    hostel_id: PyObjectId
    room_number: str
    floor: str = ""
    type: str = "double"            # single | double | triple | dormitory
    capacity: int = 2
    occupied: int = 0
    monthly_fee: float = 0
    has_ac: bool = False
    has_attached_bath: bool = False
    status: str = "available"       # available | full | maintenance

    @computed_field  # type: ignore[prop-decorator]
    @property
    def vacancies(self) -> int:
        return max(self.capacity - self.occupied, 0)


class HostelAllocation(TenantDocument):
    student_id: PyObjectId
    hostel_id: PyObjectId
    room_id: PyObjectId
    bed_number: str = ""
    academic_year_id: PyObjectId | None = None
    allocated_on: datetime | None = None
    vacated_on: datetime | None = None
    monthly_fee: float = 0
    status: str = "active"          # active | vacated | transferred
    remarks: str = ""


# ── Inventory ─────────────────────────────────────────────────────────────
class InventoryItem(TenantDocument):
    sku: str
    name: str
    category: str = "general"       # furniture | electronics | stationery | lab | sports …
    unit: str = "piece"
    quantity: float = 0
    reorder_level: float = 0
    unit_cost: float = 0
    location: str = ""
    supplier: str = ""
    purchased_on: datetime | None = None
    warranty_till: datetime | None = None
    condition: str = "good"         # good | needs_repair | damaged | disposed
    photo: FileRef | None = None
    is_asset: bool = False
    notes: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_reorder(self) -> bool:
        return self.reorder_level > 0 and self.quantity <= self.reorder_level


class InventoryTransaction(TenantDocument):
    item_id: PyObjectId
    item_name: str = ""
    type: str = "issue"             # purchase | issue | return | adjustment | disposal
    quantity: float = 0
    balance_after: float = 0
    issued_to_type: str = ""        # staff | student | department
    issued_to_id: PyObjectId | None = None
    issued_to_name: str = ""
    reference: str = ""
    handled_by: PyObjectId | None = None
    occurred_on: datetime | None = None
    remarks: str = ""


# ── Gate ──────────────────────────────────────────────────────────────────
class Visitor(TenantDocument):
    full_name: str
    phone: str = ""
    purpose: str = ""
    visiting_type: str = "staff"    # staff | student | office
    host_staff_id: PyObjectId | None = None
    host_student_id: PyObjectId | None = None
    id_proof_type: str = ""
    id_proof_number: str = ""
    persons: int = 1
    vehicle_number: str = ""
    pass_number: str = ""
    photo: FileRef | None = None
    checked_in_at: datetime | None = None
    checked_out_at: datetime | None = None
    status: str = "in"              # in | out
    remarks: str = ""
