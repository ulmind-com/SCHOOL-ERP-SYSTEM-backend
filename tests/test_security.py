"""Password handling, tokens and tenant resolution."""

from datetime import timedelta

import pytest

from app.core.config import settings
from app.core.security import (
    create_token,
    decode_token,
    fingerprint,
    generate_license_key,
    hash_password,
    password_problems,
    verify_password,
)
from app.core.tenancy import subdomain_of


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
