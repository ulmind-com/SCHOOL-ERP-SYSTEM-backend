"""Fee arithmetic. Money is the part a school will notice us getting wrong."""

from datetime import date, datetime

import pytest
from bson import ObjectId

from app.models.finance import (
    Discount,
    FeeComponent,
    FeeInvoice,
    FeeStructure,
    InvoiceLine,
    SalaryComponent,
    SalaryStructure,
    money,
)
from app.modules.fees.service import (
    _add_months,
    _collection_window,
    _due_date_for,
    _structure_covers,
    _student_is_billed_for,
    discount_for_line,
    instalment_labels,
    period_label_for,
)


class TestFeeStructure:
    def test_annual_total_multiplies_by_frequency(self):
        structure = FeeStructure(
            tenant_id=ObjectId(), name="Class 8", academic_year_id=ObjectId(),
            components=[
                FeeComponent(fee_head_id="1", amount=3500, frequency="monthly"),
                FeeComponent(fee_head_id="2", amount=15000, frequency="one_time"),
                FeeComponent(fee_head_id="3", amount=1200, frequency="half_yearly"),
            ],
        )
        # 3500*12 + 15000*1 + 1200*2
        assert structure.annual_total == 59400.0


class TestInstalments:
    def test_monthly_produces_twelve_starting_at_the_year_start(self):
        labels = instalment_labels("monthly", 4)
        assert len(labels) == 12
        assert labels[0] == ("April", 4)
        assert labels[-1] == ("March", 3)

    def test_quarterly_produces_four_three_months_apart(self):
        labels = instalment_labels("quarterly", 4)
        assert [month for _, month in labels] == [4, 7, 10, 1]

    def test_one_time_is_a_single_instalment(self):
        assert instalment_labels("one_time", 4) == [("Full Year", 4)]


class TestDiscounts:
    def test_percentage_discount(self):
        discount = Discount(tenant_id=ObjectId(), code="M10", name="Merit",
                            type="percentage", value=10)
        assert discount.apply_to(3500) == 350.0

    def test_fixed_discount_never_exceeds_the_line(self):
        discount = Discount(tenant_id=ObjectId(), code="F", name="Fixed",
                            type="fixed", value=5000)
        assert discount.apply_to(3500) == 3500.0

    def test_stacked_discounts_are_capped_at_the_line_amount(self):
        stacked = [
            {"type": "percentage", "value": 60, "fee_head_ids": []},
            {"type": "percentage", "value": 80, "fee_head_ids": []},
        ]
        assert discount_for_line(stacked, None, 1000) == 1000.0

    def test_discount_scoped_to_a_head_skips_other_heads(self):
        head = ObjectId()
        other = ObjectId()
        scoped = [{"type": "percentage", "value": 50, "fee_head_ids": [head]}]
        assert discount_for_line(scoped, head, 1000) == 500.0
        assert discount_for_line(scoped, other, 1000) == 0.0


class TestFacilityBilling:
    """Transport and hostel follow the facility, not the fee structure."""

    def test_transport_only_bills_students_who_use_it(self):
        component = {"fee_head_name": "Transport Fee", "is_optional": False}
        assert _student_is_billed_for({"uses_transport": True}, component)
        assert not _student_is_billed_for({"uses_transport": False}, component)
        assert not _student_is_billed_for({}, component)

    def test_hostel_only_bills_hostellers(self):
        component = {"fee_head_name": "Hostel Fee", "is_optional": False}
        assert _student_is_billed_for({"is_hosteller": True}, component)
        assert not _student_is_billed_for({"is_hosteller": False}, component)

    def test_ordinary_heads_bill_everyone_unless_marked_optional(self):
        assert _student_is_billed_for({}, {"fee_head_name": "Tuition Fee"})
        assert not _student_is_billed_for(
            {}, {"fee_head_name": "Music Club", "is_optional": True}
        )


class TestInvoice:
    def test_line_net_subtracts_discount_and_adds_tax(self):
        line = InvoiceLine(description="Tuition", amount=3500, discount=350, tax=0)
        assert line.net == 3150.0

    def test_balance_never_goes_negative_on_overpayment(self):
        invoice = FeeInvoice(
            tenant_id=ObjectId(), number="INV-1", student_id=ObjectId(),
            total=5000, paid_amount=6000,
        )
        assert invoice.balance == 0.0

    def test_balance_reflects_part_payment(self):
        invoice = FeeInvoice(
            tenant_id=ObjectId(), number="INV-1", student_id=ObjectId(),
            total=5000, paid_amount=1750.5,
        )
        assert invoice.balance == 3249.5


class TestPayroll:
    def test_percent_of_basic_components_resolve_against_basic(self):
        structure = SalaryStructure(
            tenant_id=ObjectId(), basic=40000,
            components=[
                SalaryComponent(name="HRA", calculation="percent_of_basic", value=40),
                SalaryComponent(name="PF", type="deduction",
                                calculation="percent_of_basic", value=12),
                SalaryComponent(name="Conveyance", value=2000),
            ],
        )
        result = structure.compute()
        assert result["gross"] == 58000.0       # 40000 + 16000 + 2000
        assert result["deductions"] == 4800.0   # 12% of 40000
        assert result["net"] == 53200.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 0.0), (1000, 1000.0), (1000.456, 1000.46), (0.005, 0.01), (-5.554, -5.55)],
)
def test_money_rounds_to_paise(value, expected):
    assert money(value) == expected


class TestBillingCycles:
    """A structure carries lines on different schedules; a run bills one."""

    def test_semester_counts_as_two_instalments(self):
        structure = FeeStructure(
            tenant_id=ObjectId(), name="BSc Year 1", academic_year_id=ObjectId(),
            components=[FeeComponent(fee_head_id="1", amount=40000, frequency="semester")],
        )
        assert structure.annual_total == 80000

    def test_period_label_names_the_month(self):
        assert period_label_for("monthly", date(2026, 9, 14)) == "September 2026"

    def test_period_label_names_the_semester(self):
        assert period_label_for("semester", date(2026, 9, 14)) == "Semester 2 · 2026"
        assert period_label_for("semester", date(2026, 3, 1), 1) == "Semester 1 · 2026"

    def test_every_cycle_gets_its_own_label_on_the_same_day(self):
        """Two runs on the same day must not collide on the skip check —
        a colliding label makes the second run silently bill nobody."""
        on = date(2026, 4, 2)
        labels = [
            period_label_for(cycle, on)
            for cycle in ("all", "one_time", "monthly", "quarterly", "half_yearly",
                          "semester", "yearly")
        ]
        assert len(set(labels)) == len(labels), labels

    def test_monthly_due_date_follows_the_components_due_day(self):
        components = [{"due_day": 10}, {"due_day": 10}]
        due = _due_date_for("monthly", components, date(2026, 9, 1), date(2026, 9, 30))
        assert due == date(2026, 9, 10)

    def test_due_day_is_ignored_when_components_disagree(self):
        components = [{"due_day": 5}, {"due_day": 20}]
        fallback = date(2026, 9, 30)
        assert _due_date_for("monthly", components, date(2026, 9, 1), fallback) == fallback


class TestStructureMatching:
    """A school bills by class, a college by programme — one structure type."""

    def _student(self, **kwargs):
        return {"current_class_id": None, "program_id": None, "department_id": None,
                "semester": None, **kwargs}

    def test_a_class_structure_matches_that_class(self):
        structure = {"class_ids": ["c1"]}
        assert _structure_covers(structure, self._student(current_class_id="c1"))
        assert not _structure_covers(structure, self._student(current_class_id="c2"))

    def test_a_programme_structure_matches_that_programme(self):
        structure = {"program_ids": ["cse"]}
        assert _structure_covers(structure, self._student(program_id="cse"))
        assert not _structure_covers(structure, self._student(program_id="it"))

    def test_a_department_structure_matches_that_department(self):
        structure = {"department_ids": ["d1"]}
        assert _structure_covers(structure, self._student(department_id="d1"))

    def test_naming_nothing_covers_everyone(self):
        """An institution with one fee book should not have to list its classes."""
        assert _structure_covers({}, self._student(current_class_id="anything"))

    def test_a_semester_structure_narrows_further(self):
        structure = {"program_ids": ["cse"], "semesters": [3, 4]}
        assert _structure_covers(structure, self._student(program_id="cse", semester=3))
        assert not _structure_covers(structure, self._student(program_id="cse", semester=1))

    def test_the_old_single_programme_field_still_counts(self):
        """Structures written before the list existed must keep working."""
        assert _structure_covers({"program_id": "cse"}, self._student(program_id="cse"))


class TestInstalmentDates:
    def test_months_advance_across_a_year_boundary(self):
        assert _add_months(date(2026, 11, 10), 3) == date(2027, 2, 10)

    def test_a_due_day_of_31_clamps_to_the_shorter_month(self):
        """Otherwise the instalment after January has no date at all."""
        assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_february_in_a_leap_year(self):
        assert _add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)


class TestCollectionWindow:
    """A school that opens an examination fee for a fortnight means it."""

    def test_no_window_is_always_open(self):
        assert _collection_window({}, date(2026, 9, 10)) == (None, None)

    def test_relative_offsets_hang_off_the_due_date(self):
        opens, closes = _collection_window(
            {"opens_days_before": 7, "closes_days_after": 3}, date(2026, 9, 10)
        )
        assert opens == date(2026, 9, 3)
        assert closes == date(2026, 9, 13)

    def test_fixed_dates_win_over_offsets(self):
        """An exam fee is tied to the exam, not to a due date that may move."""
        opens, closes = _collection_window(
            {
                "opens_days_before": 7,
                "collect_from": datetime(2026, 11, 1),
                "collect_until": datetime(2026, 11, 15),
            },
            date(2026, 9, 10),
        )
        assert opens == date(2026, 11, 1)
        assert closes == date(2026, 11, 15)

    def test_one_sided_windows(self):
        assert _collection_window({"closes_days_after": 5}, date(2026, 9, 10)) == (
            None,
            date(2026, 9, 15),
        )
