"""Password handling, tokens and tenant resolution."""

import asyncio
from datetime import timedelta

import pytest
from bson import ObjectId

from app.core.config import settings
from app.core.context import AuthContext, TenantContext
from app.core.scoping import student_row_scope
from app.core.security import (
    create_token,
    decode_token,
    fingerprint,
    generate_license_key,
    hash_password,
    password_problems,
    phone_digits,
    verify_password,
)
from app.core.tenancy import subdomain_of
from app.modules.auth.service import _identifier_query


def _tenant() -> TenantContext:
    return TenantContext(id=ObjectId(), slug="x", name="X")


class TestPasswords:
    def test_round_trip(self):
        hashed = hash_password("Scholarly@2026")
        assert verify_password("Scholarly@2026", hashed)
        assert not verify_password("scholarly@2026", hashed)

    def test_each_hash_is_salted_differently(self):
        assert hash_password("same") != hash_password("same")

    def test_long_passphrases_are_not_truncated_at_72_bytes(self):
        """bcrypt silently truncates; we pre-hash, so these must differ."""
        base = "a" * 72
        hashed = hash_password(base + "ONE")
        assert verify_password(base + "ONE", hashed)
        assert not verify_password(base + "TWO", hashed)

    def test_empty_hash_never_verifies(self):
        assert not verify_password("anything", "")

    def test_malformed_hash_is_rejected_not_raised(self):
        assert not verify_password("anything", "not-a-bcrypt-hash")

    @pytest.mark.parametrize(
        ("password", "problem"),
        [
            ("Sh0rt", "at least"),
            ("alllowercase1", "uppercase"),
            ("ALLUPPERCASE1", "lowercase"),
            ("NoDigitsHere", "number"),
        ],
    )
    def test_weak_passwords_are_described(self, password, problem):
        problems = password_problems(password)
        assert any(problem in p for p in problems)

    def test_a_good_password_has_no_problems(self):
        assert password_problems("Scholarly@2026") == []


class TestTokens:
    def test_access_token_round_trip(self):
        token = create_token("user-1", "access", tenant_id="tenant-1")
        payload = decode_token(token, "access")
        assert payload["sub"] == "user-1"
        assert payload["tid"] == "tenant-1"
        assert payload["scope"] == "tenant"

    def test_token_type_is_enforced(self):
        """A refresh token must not be usable as an access token."""
        refresh = create_token("user-1", "refresh")
        assert decode_token(refresh, "refresh") is not None
        assert decode_token(refresh, "access") is None

    def test_expired_token_is_rejected(self):
        token = create_token("user-1", "access", expires_delta=timedelta(seconds=-10))
        assert decode_token(token) is None

    def test_tampered_token_is_rejected(self):
        token = create_token("user-1", "access")
        assert decode_token(token[:-3] + "aaa") is None

    def test_garbage_is_rejected(self):
        assert decode_token("not.a.token") is None

    def test_each_token_has_a_unique_id(self):
        a = decode_token(create_token("u", "access"))
        b = decode_token(create_token("u", "access"))
        assert a["jti"] != b["jti"]

    def test_platform_scope_is_carried(self):
        payload = decode_token(create_token("u", "access", scope="platform"))
        assert payload["scope"] == "platform"


class TestFingerprints:
    def test_is_stable_for_the_same_input(self):
        assert fingerprint("token") == fingerprint("token")

    def test_differs_between_inputs(self):
        assert fingerprint("a") != fingerprint("b")

    def test_is_not_the_token_itself(self):
        assert fingerprint("token") != "token"

    def test_licence_keys_are_deterministic_and_shaped(self):
        key = generate_license_key("st-xavier")
        assert key == generate_license_key("st-xavier")
        assert key != generate_license_key("other-school")
        assert key.startswith("SCHLY-")
        assert len(key.split("-")) == 5


class TestSubdomainResolution:
    @pytest.fixture(autouse=True)
    def _base_domain(self):
        original = settings.tenant_base_domain
        settings.tenant_base_domain = "scholarly.app"
        yield
        settings.tenant_base_domain = original

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("stjohns.scholarly.app", "stjohns"),
            ("stjohns.scholarly.app:443", "stjohns"),
            ("STJOHNS.SCHOLARLY.APP", "stjohns"),
            ("scholarly.app", None),          # the apex is not a tenant
            ("www.scholarly.app", None),      # reserved
            ("api.scholarly.app", None),      # reserved
            ("a.b.scholarly.app", None),      # nested, ambiguous
            ("stjohns.evil.com", None),       # different domain entirely
            ("", None),
        ],
    )
    def test_resolution(self, host, expected):
        assert subdomain_of(host) == expected

    def test_disabled_when_no_base_domain_is_configured(self):
        settings.tenant_base_domain = ""
        assert subdomain_of("stjohns.scholarly.app") is None


class TestRowScoping:
    """Permissions say *whether*; these hooks say *whose*.

    A student and a parent both legitimately hold ``invoices:read``. What keeps
    the portal safe is the query narrowing that happens before Mongo sees it.
    """

    def _auth(self, **kwargs) -> AuthContext:
        return AuthContext(user_id=ObjectId(), email="a@b.c", full_name="T", **kwargs)

    def test_a_student_is_narrowed_to_themselves(self):
        student_id = ObjectId()
        auth = self._auth(portal="student", student_id=student_id)
        scope = asyncio.run(student_row_scope(auth, _tenant()))
        assert scope == {"student_id": {"$in": [student_id]}}

    def test_staff_are_not_narrowed_at_all(self):
        auth = self._auth(portal="admin", permissions=["*"])
        assert asyncio.run(student_row_scope(auth, _tenant())) == {}

    def test_a_portal_account_with_no_links_sees_nothing(self):
        """The dangerous case: an empty filter would mean *everything*."""
        auth = self._auth(portal="parent")
        scope = asyncio.run(student_row_scope(auth, _tenant()))
        assert scope == {"student_id": {"$in": []}}

    def test_family_scoped_endpoints_pass_the_narrowing_down(self):
        """A register, a loan list and a homework list all read the same hook —
        each one that forgets it hands over the whole class."""
        import inspect

        from app.modules.attendance.router import get_register
        from app.modules.library.router import loans
        from app.modules.lms.service import my_assignments

        for fn in (get_register, loans, my_assignments):
            assert "family_student_ids" in inspect.getsource(fn), fn.__name__

    def test_a_family_is_not_handed_the_class_s_assignment_list(self):
        """``assignments:read`` is what shows a parent their own child's
        homework. Read unscoped it was the whole institution's — every class,
        and the drafts a teacher was still writing."""
        from app.core.scoping import family_assignment_scope

        auth = self._auth(portal="parent")
        scope = asyncio.run(family_assignment_scope(auth, _tenant()))
        assert scope == {"_id": {"$in": []}}

        staff = self._auth(portal="admin", permissions=["*"])
        assert asyncio.run(family_assignment_scope(staff, _tenant())) == {}

    def test_the_assignments_resource_is_scoped(self):
        from app.core.scoping import family_assignment_scope
        from app.modules.registry import RESOURCES

        assignments = next(r for r in RESOURCES if r.name == "assignments")
        assert assignments.scope_hook is family_assignment_scope

    def test_the_submission_roster_is_staff_only(self):
        """It carries every classmate's name, whether they handed it in and what
        they scored — which is not what a parent's read permission is for."""
        import inspect

        from app.modules.lms.router import submissions

        assert "assert_not_family" in inspect.getsource(submissions)

    def test_every_student_keyed_resource_carries_the_hook(self):
        """A new resource keyed by student must not ship without scoping."""
        from app.modules.fees.router import INVOICES, PAYMENTS
        from app.modules.people.router import ENROLLMENTS, STUDENTS

        for resource in (INVOICES, PAYMENTS, ENROLLMENTS, STUDENTS):
            assert resource.scope_hook is not None, resource.name


class TestLoginIdentifier:
    """A parent remembers the number the school already has, not an address
    the office invented for them — so both have to work."""

    def test_an_email_matches_on_email_only(self):
        assert _identifier_query("Head@Valley.Demo") == {"email": "head@valley.demo"}

    def test_a_phone_number_matches_on_either(self):
        query = _identifier_query("98765 43210")
        assert query["$or"] == [{"email": "98765 43210"}, {"phone_digits": "9876543210"}]

    def test_a_country_code_is_ignored(self):
        assert _identifier_query("+91 98765 43210")["$or"][1] == {"phone_digits": "9876543210"}

    def test_something_too_short_is_treated_as_an_email(self):
        """Otherwise a typo'd address would silently become a phone lookup."""
        assert _identifier_query("abc12") == {"email": "abc12"}


class TestPhoneDigits:
    def test_separators_and_country_code_are_stripped(self):
        assert phone_digits("+91 98765-43210") == "9876543210"

    def test_a_short_number_is_not_stored(self):
        """A three-digit extension is not something to match a login on."""
        assert phone_digits("123") == ""

    def test_missing_is_empty(self):
        assert phone_digits(None) == ""
