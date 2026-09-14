"""Fee arithmetic. Money is the part a school will notice us getting wrong."""

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
    _student_is_billed_for,
    discount_for_line,
    instalment_labels,
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
