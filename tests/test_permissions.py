"""Permission resolution — the thing that decides what each role can do."""

from bson import ObjectId

from app.core.context import AuthContext, PlanLimits, TenantContext
from app.core.navigation import NAVIGATION, build_navigation
from app.core.permissions import (
    ALL_PERMISSIONS,
    MODULES_BY_KEY,
    ROLE_PRESETS_BY_KEY,
    expand,
    has_permission,
)


def test_wildcard_grants_everything():
    assert has_permission(["*"], "students:delete")
    assert has_permission(["*"], "payroll:approve")


def test_module_wildcard_is_scoped_to_that_module():
    granted = ["students:*"]
    assert has_permission(granted, "students:delete")
    assert not has_permission(granted, "staff:delete")


def test_exact_permission_does_not_leak_to_siblings():
    granted = ["students:read"]
    assert has_permission(granted, "students:read")
    assert not has_permission(granted, "students:update")


def test_empty_grants_nothing():
    assert not has_permission([], "dashboard:read")
    assert not has_permission(set(), "students:read")


def test_expand_resolves_wildcards_to_concrete_permissions():
    expanded = expand(["students:*"])
    assert expanded == set(MODULES_BY_KEY["students"].permissions)
    assert "students:export" in expanded


def test_expand_star_is_every_permission():
    assert expand(["*"]) == set(ALL_PERMISSIONS)


def test_unknown_module_wildcard_is_ignored_not_expanded():
    assert expand(["nonsense:*"]) == set()


class TestRolePresets:
    def _auth(self, key: str) -> AuthContext:
        preset = ROLE_PRESETS_BY_KEY[key]
        return AuthContext(
            user_id=ObjectId(),
            email="a@b.c",
            full_name="Test",
            permissions=expand(preset.permissions) | set(preset.permissions),
            portal=preset.portal,
            is_owner=preset.is_owner,
        )

    def test_teacher_cannot_touch_money(self):
        teacher = self._auth("teacher")
        assert teacher.can("attendance:create")
        assert not teacher.can("payments:collect")
        assert not teacher.can("invoices:create")
        assert not teacher.can("users:create")

    def test_accountant_cannot_take_attendance(self):
        accountant = self._auth("accountant")
        assert accountant.can("payments:collect")
        assert accountant.can("invoices:create")
        assert not accountant.can("attendance:create")
        assert not accountant.can("exams:update")

    def test_parent_is_read_only_on_their_children(self):
        parent = self._auth("parent")
        assert parent.can("attendance:read")
        assert parent.can("results:read")
        assert not parent.can("attendance:create")
        assert not parent.can("students:read")

    def test_student_cannot_see_other_students(self):
        student = self._auth("student")
        assert student.can("results:read")
        assert not student.can("students:read")
        assert not student.can("guardians:read")

    def test_super_admin_holds_everything(self):
        owner = self._auth("super_admin")
        assert owner.is_owner
        for permission in ALL_PERMISSIONS:
            assert owner.can(permission), permission


class TestNavigation:
    def _tenant(self, **kwargs) -> TenantContext:
        return TenantContext(id=ObjectId(), slug="x", name="X", **kwargs)

    def test_dedicated_deployment_sees_every_nav_item(self):
        """An owner on a dedicated deployment has nothing withheld — so the
        rendered menu should match the definition exactly, whatever its size."""
        tenant = self._tenant(deployment="dedicated")
        owner = AuthContext(
            user_id=ObjectId(), email="a@b.c", full_name="T",
            permissions=expand(["*"]), is_owner=True,
        )
        rendered = {i["key"] for g in build_navigation(owner, tenant) for i in g["items"]}
        defined = {item.key for group in NAVIGATION for item in group.items}
        assert rendered == defined

    def test_saas_plan_hides_modules_it_does_not_include(self):
        tenant = self._tenant(deployment="saas", enabled_modules={"students", "guardians"})
        owner = AuthContext(
            user_id=ObjectId(), email="a@b.c", full_name="T",
            permissions=expand(["*"]), is_owner=True,
        )
        keys = {i["key"] for g in build_navigation(owner, tenant) for i in g["items"]}
        assert "students" in keys
        assert "transport" not in keys
        assert "payroll" not in keys

    def test_core_modules_are_never_gated_by_a_plan(self):
        tenant = self._tenant(deployment="saas", enabled_modules=set())
        owner = AuthContext(
            user_id=ObjectId(), email="a@b.c", full_name="T", permissions=expand(["*"]),
        )
        keys = {i["key"] for g in build_navigation(owner, tenant) for i in g["items"]}
        # Dashboard, settings, users and attendance are not purchasable extras.
        assert {"dashboard", "settings", "users", "attendance"} <= keys

    def test_permissions_and_modules_both_have_to_allow_an_item(self):
        tenant = self._tenant(deployment="dedicated")
        teacher_preset = ROLE_PRESETS_BY_KEY["teacher"]
        teacher = AuthContext(
            user_id=ObjectId(), email="a@b.c", full_name="T",
            permissions=expand(teacher_preset.permissions) | set(teacher_preset.permissions),
            portal="teacher",
        )
        keys = {i["key"] for g in build_navigation(teacher, tenant) for i in g["items"]}
        assert "attendance" in keys
        assert "payroll" not in keys      # module is on; permission is not held
        assert "users" not in keys


class TestPlanLimits:
    def test_dedicated_limits_are_unlimited(self):
        limits = PlanLimits.unlimited()
        assert limits.ceiling("max_students") is None
        assert limits.ceiling("max_staff") is None

    def test_dedicated_tenant_enables_every_module_regardless_of_list(self):
        tenant = TenantContext(
            id=ObjectId(), slug="x", name="X", deployment="dedicated", enabled_modules=set()
        )
        assert tenant.module_enabled("payroll")
        assert tenant.module_enabled("transport")

    def test_suspended_tenant_is_not_usable(self):
        assert not TenantContext(id=ObjectId(), slug="x", name="X", status="suspended").is_usable
        assert TenantContext(id=ObjectId(), slug="x", name="X", status="trial").is_usable

    def test_storage_path_separates_dedicated_from_shared(self):
        shared = TenantContext(id=ObjectId(), slug="green", name="G")
        owned = TenantContext(id=ObjectId(), slug="green", name="G", deployment="dedicated")
        assert shared.storage_path() == "/tenants/green"
        assert owned.storage_path() == "/dedicated/green"
