"""Library fines, payroll arithmetic and payment verification."""

from datetime import date

import pytest
from bson import ObjectId

from app.core.context import AuthContext, TenantContext
from app.core.permissions import ROLE_PRESETS_BY_KEY, expand
from app.modules.ai.service import FAMILY_PORTALS, tools_for
from app.modules.library.service import _rules, fine_for
from app.modules.payroll.service import compute_payslip, period_bounds, working_days_in


class TestLibraryFines:
    def test_returned_early_is_free(self):
        assert fine_for(date(2026, 9, 20), date(2026, 9, 18), 2.0) == (0, 0.0)

    def test_returned_on_the_due_date_is_free(self):
        assert fine_for(date(2026, 9, 18), date(2026, 9, 18), 2.0) == (0, 0.0)

    def test_late_accrues_per_day(self):
        assert fine_for(date(2026, 9, 15), date(2026, 9, 18), 2.0) == (3, 6.0)

    def test_no_due_date_cannot_be_late(self):
        assert fine_for(None, date(2026, 9, 18), 2.0) == (0, 0.0)

    def test_institution_can_override_the_rules(self):
        tenant = TenantContext(
            id=ObjectId(), slug="x", name="X",
            settings={"library": {"loan_days": 21, "fine_per_day": 5, "max_renewals": 4}},
        )
        rules = _rules(tenant)
        assert rules["loan_days"] == 21
        assert rules["fine_per_day"] == 5.0
        assert rules["max_renewals"] == 4

    def test_defaults_when_unset(self):
        rules = _rules(TenantContext(id=ObjectId(), slug="x", name="X"))
        assert rules["loan_days"] == 14
        assert rules["max_books_student"] == 2


class TestPayrollPeriods:
    def test_month_bounds(self):
        assert period_bounds(2026, 9) == (date(2026, 9, 1), date(2026, 9, 30))
        assert period_bounds(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))

    def test_leap_february(self):
        assert period_bounds(2028, 2)[1] == date(2028, 2, 29)

    def test_six_day_week_is_the_default(self):
        """Indian schools commonly work Monday to Saturday."""
        assert working_days_in(2026, 9) == 26

    def test_five_day_week_can_be_configured(self):
        both = working_days_in(2026, 9, week_off=7) - 4  # minus the Saturdays
        assert both == 22


class TestPayslipArithmetic:
    def _slip(self, **overrides):
        defaults = {
            "basic": 40000,
            "components": [
                {"name": "HRA", "calculation": "percent_of_basic", "value": 40,
                 "type": "earning"},
                {"name": "Conveyance", "value": 2000, "type": "earning"},
                {"name": "PF", "calculation": "percent_of_basic", "value": 12,
                 "type": "deduction"},
            ],
            "working_days": 26, "present_days": 26, "lop_days": 0,
        }
        return compute_payslip(**{**defaults, **overrides})

    def test_gross_includes_every_earning(self):
        assert self._slip()["gross"] == 58000.0

    def test_percent_components_resolve_against_basic(self):
        slip = self._slip()
        assert slip["total_deductions"] == 4800.0

    def test_full_attendance_has_no_loss_of_pay(self):
        slip = self._slip()
        assert slip["net_pay"] == 53200.0
        assert not any("Loss of pay" in d["name"] for d in slip["deductions"])

    def test_loss_of_pay_is_taken_from_gross_not_basic(self):
        """A day not worked costs the allowances too."""
        slip = self._slip(lop_days=2, present_days=24)
        lop = next(d for d in slip["deductions"] if "Loss of pay" in d["name"])
        assert lop["amount"] == pytest.approx(58000 / 26 * 2, abs=0.01)
        assert slip["net_pay"] == pytest.approx(58000 - 4800 - lop["amount"], abs=0.01)

    def test_zero_working_days_does_not_divide_by_zero(self):
        slip = self._slip(working_days=0, lop_days=3)
        assert slip["net_pay"] == 53200.0

    def test_basic_only(self):
        slip = compute_payslip(basic=30000, components=[], working_days=26,
                               present_days=26, lop_days=0)
        assert slip["gross"] == 30000.0
        assert slip["net_pay"] == 30000.0


class TestAssistantScoping:
    """The assistant must not become a way around row-level access."""

    def _auth(self, key: str) -> AuthContext:
        preset = ROLE_PRESETS_BY_KEY[key]
        return AuthContext(
            user_id=ObjectId(), email="a@b.c", full_name="Test",
            permissions=expand(preset.permissions), portal=preset.portal,
        )

    def _tenant(self) -> TenantContext:
        return TenantContext(id=ObjectId(), slug="x", name="X", deployment="dedicated")

    @pytest.mark.parametrize("role", ["parent", "student"])
    def test_families_get_only_their_own_children(self, role):
        tools = tools_for(self._auth(role), self._tenant())
        assert [t["name"] for t in tools] == ["my_children"]

    def test_family_portals_are_the_ones_we_think(self):
        assert FAMILY_PORTALS == {"student", "parent"}

    def test_teacher_has_no_fee_tool(self):
        names = [t["name"] for t in tools_for(self._auth("teacher"), self._tenant())]
        assert "fee_summary" not in names
        assert "attendance_summary" in names

    def test_accountant_has_no_exam_tool(self):
        names = [t["name"] for t in tools_for(self._auth("accountant"), self._tenant())]
        assert "fee_summary" in names
        assert "exam_results" not in names

    def test_owner_gets_the_institution_wide_set(self):
        names = [t["name"] for t in tools_for(self._auth("super_admin"), self._tenant())]
        assert "institution_overview" in names
        assert "fee_summary" in names

    def test_a_disabled_module_removes_its_tool(self):
        tenant = TenantContext(
            id=ObjectId(), slug="x", name="X", deployment="saas",
            enabled_modules={"students", "attendance"},
        )
        names = [t["name"] for t in tools_for(self._auth("super_admin"), tenant)]
        assert "exam_results" not in names


class TestPaymentVerification:
    """Signature checking is the only thing standing between a receipt and a
    forged one, so it is tested directly."""

    def _gateway(self, monkeypatch):
        from app.core.config import settings
        from app.modules.payments.gateway import RazorpayGateway

        monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
        monkeypatch.setattr(settings, "razorpay_key_secret", "test-secret")
        monkeypatch.setattr(settings, "razorpay_webhook_secret", "hook-secret")
        return RazorpayGateway()

    def _sign(self, secret: str, payload: str) -> str:
        import hashlib
        import hmac

        return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def test_valid_signature_passes(self, monkeypatch):
        gateway = self._gateway(monkeypatch)
        signature = self._sign("test-secret", "order_1|pay_1")
        assert gateway.verify_signature(
            order_id="order_1", payment_id="pay_1", signature=signature
        )

    def test_tampered_signature_fails(self, monkeypatch):
        gateway = self._gateway(monkeypatch)
        assert not gateway.verify_signature(
            order_id="order_1", payment_id="pay_1", signature="deadbeef"
        )

    def test_signature_is_bound_to_the_order(self, monkeypatch):
        """A signature for one order must not validate another."""
        gateway = self._gateway(monkeypatch)
        signature = self._sign("test-secret", "order_1|pay_1")
        assert not gateway.verify_signature(
            order_id="order_2", payment_id="pay_1", signature=signature
        )

    def test_empty_signature_fails(self, monkeypatch):
        gateway = self._gateway(monkeypatch)
        assert not gateway.verify_signature(order_id="o", payment_id="p", signature="")

    def test_webhook_uses_its_own_secret(self, monkeypatch):
        gateway = self._gateway(monkeypatch)
        body = b'{"event":"payment.captured"}'
        assert gateway.verify_webhook(
            body=body, signature=self._sign("hook-secret", body.decode())
        )
        assert not gateway.verify_webhook(
            body=body, signature=self._sign("test-secret", body.decode())
        )

    def test_unconfigured_gateway_refuses_to_construct(self):
        from app.modules.payments.gateway import PaymentsDisabled, RazorpayGateway

        with pytest.raises(PaymentsDisabled):
            RazorpayGateway()
