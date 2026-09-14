"""Document plumbing: ids, dates and the shapes that cross the wire."""

from datetime import UTC, date, datetime

import pytest
from bson import ObjectId

from app.core.crud import make_create_schema, make_update_schema
from app.models.academics import GradeBand, GradeScale
from app.models.base import Page, bsonify, oid, serialize_doc, to_datetime
from app.models.operations import percentage_of
from app.models.people import Student, age_on
from app.models.tenant import Tenant


class TestObjectIdHandling:
    def test_model_dump_keeps_objectid_for_mongo(self):
        """The bug this guards: a str foreign key writes fine and matches nothing."""
        student = Student(
            tenant_id=ObjectId(), admission_number="A1", first_name="Riya",
            current_class_id=ObjectId(),
        )
        dumped = student.model_dump()
        assert isinstance(dumped["current_class_id"], ObjectId)
        assert isinstance(dumped["tenant_id"], ObjectId)

    def test_json_dump_stringifies_for_the_wire(self):
        student = Student(tenant_id=ObjectId(), admission_number="A1", first_name="Riya")
        assert isinstance(student.model_dump(mode="json")["tenant_id"], str)

    def test_string_ids_are_accepted_and_coerced(self):
        raw = "6aa6bf077679e658505cda78"
        student = Student(
            tenant_id=ObjectId(), admission_number="A1", first_name="R", current_class_id=raw
        )
        assert student.current_class_id == ObjectId(raw)

    def test_invalid_id_is_rejected(self):
        with pytest.raises(ValueError):
            Student(tenant_id=ObjectId(), admission_number="A", first_name="R",
                    current_class_id="not-an-id")

    def test_oid_helper_is_forgiving(self):
        assert oid(None) is None
        assert oid("nonsense") is None
        assert isinstance(oid(str(ObjectId())), ObjectId)


class TestBsonSafety:
    def test_dates_are_promoted_to_datetimes(self):
        out = bsonify({"d": date(2026, 9, 27)})
        assert isinstance(out["d"], datetime)
        assert out["d"] == datetime(2026, 9, 27, tzinfo=UTC)

    def test_nested_structures_are_walked(self):
        out = bsonify({"a": [{"b": date(2026, 1, 1)}], "c": (date(2026, 2, 2),)})
        assert isinstance(out["a"][0]["b"], datetime)
        assert isinstance(out["c"][0], datetime)

    def test_datetimes_pass_through_untouched(self):
        now = datetime.now(UTC)
        assert bsonify({"t": now})["t"] is now

    def test_to_datetime_handles_none(self):
        assert to_datetime(None) is None


class TestSerialisation:
    def test_underscore_id_becomes_id(self):
        doc = {"_id": ObjectId(), "name": "x"}
        assert "id" in serialize_doc(doc)
        assert "_id" not in serialize_doc(doc)

    def test_nested_ids_and_dates_are_json_safe(self):
        out = serialize_doc({
            "_id": ObjectId(),
            "when": datetime.now(UTC),
            "day": date(2026, 1, 1),
            "kids": [{"_id": ObjectId()}],
        })
        assert isinstance(out["kids"][0]["id"], str)
        assert isinstance(out["when"], str)
        assert out["day"] == "2026-01-01"

    def test_none_stays_none(self):
        assert serialize_doc(None) is None


class TestPagination:
    def test_middle_page_has_both_neighbours(self):
        page = Page[dict].build([{"a": 1}], total=51, page=2, page_size=20)
        assert page.meta.total_pages == 3
        assert page.meta.has_next and page.meta.has_prev

    def test_single_page_has_neither(self):
        page = Page[dict].build([], total=0, page=1, page_size=25)
        assert page.meta.total_pages == 1
        assert not page.meta.has_next and not page.meta.has_prev

    def test_exact_multiple_does_not_add_an_empty_page(self):
        page = Page[dict].build([], total=40, page=2, page_size=20)
        assert page.meta.total_pages == 2
        assert not page.meta.has_next


class TestDerivedSchemas:
    def test_create_schema_drops_server_owned_fields(self):
        schema = make_create_schema(Student)
        assert "tenant_id" not in schema.model_fields
        assert "created_at" not in schema.model_fields
        assert "is_deleted" not in schema.model_fields
        assert "first_name" in schema.model_fields

    def test_generated_fields_become_optional_on_the_request(self):
        """A hook fills these in, so the client must not be asked for them."""
        schema = make_create_schema(Student, optional={"admission_number"})
        instance = schema(first_name="Riya")
        assert instance.admission_number is None

    def test_update_schema_is_entirely_optional(self):
        schema = make_update_schema(Student)
        assert all(field.default is None for field in schema.model_fields.values())
        assert schema(first_name="Riya").model_dump(exclude_unset=True) == {"first_name": "Riya"}


class TestGrading:
    def _scale(self) -> GradeScale:
        return GradeScale(
            tenant_id=ObjectId(), name="Default",
            bands=[
                GradeBand(grade="A", min=80, max=100, points=10),
                GradeBand(grade="B", min=60, max=79.99, points=8),
                GradeBand(grade="F", min=0, max=59.99, points=0),
            ],
        )

    @pytest.mark.parametrize(
        ("percentage", "grade"), [(100, "A"), (80, "A"), (79.99, "B"), (60, "B"), (0, "F")]
    )
    def test_band_boundaries(self, percentage, grade):
        assert self._scale().grade_for(percentage).grade == grade

    def test_out_of_range_returns_nothing(self):
        assert self._scale().grade_for(120) is None

    def test_percentage_of_guards_against_zero_max(self):
        assert percentage_of(50, 0) is None
        assert percentage_of(None, 100) is None
        assert percentage_of(45, 60) == 75.0


class TestTenantSlug:
    def test_slug_is_lowercased_and_trimmed(self):
        assert Tenant(slug="  St-Johns  ", name="X").slug == "st-johns"

    @pytest.mark.parametrize("slug", ["admin", "api", "www", "platform", "billing"])
    def test_reserved_slugs_are_refused(self, slug):
        with pytest.raises(ValueError):
            Tenant(slug=slug, name="X")

    @pytest.mark.parametrize("slug", ["has space", "under_score", "punct!", ""])
    def test_malformed_slugs_are_refused(self, slug):
        with pytest.raises(ValueError):
            Tenant(slug=slug, name="X")


class TestAge:
    def test_birthday_not_yet_reached_this_year(self):
        assert age_on(datetime(2012, 12, 31, tzinfo=UTC), date(2026, 6, 1)) == 13

    def test_birthday_already_passed(self):
        assert age_on(datetime(2012, 1, 1, tzinfo=UTC), date(2026, 6, 1)) == 14

    def test_birthday_today(self):
        assert age_on(datetime(2012, 6, 1, tzinfo=UTC), date(2026, 6, 1)) == 14

    def test_missing_date_of_birth(self):
        assert age_on(None) is None
