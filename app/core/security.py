"""Password hashing and JWT issuing/verification."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt
from jwt import InvalidTokenError

from app.core.config import settings

TokenType = Literal["access", "refresh", "invite", "reset"]

# bcrypt silently truncates at 72 bytes; pre-hash so long passphrases stay safe.
_BCRYPT_LIMIT = 72


def _prepare(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_LIMIT:
        raw = hashlib.sha256(raw).hexdigest().encode("utf-8")
    return raw


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_prepare(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def password_problems(password: str) -> list[str]:
    """Human-readable reasons a password is unacceptable (empty list = fine)."""
    issues: list[str] = []
    if len(password) < settings.password_min_length:
        issues.append(f"must be at least {settings.password_min_length} characters")
    if not any(c.islower() for c in password):
        issues.append("must contain a lowercase letter")
    if not any(c.isupper() for c in password):
        issues.append("must contain an uppercase letter")
    if not any(c.isdigit() for c in password):
        issues.append("must contain a number")
    return issues


# ── JWT ───────────────────────────────────────────────────────────────────
def create_token(
    subject: str,
    token_type: TokenType = "access",
    *,
    tenant_id: str | None = None,
    scope: str = "tenant",
    expires_delta: timedelta | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    if expires_delta is None:
        expires_delta = {
            "access": timedelta(minutes=settings.access_token_expire_minutes),
            "refresh": timedelta(days=settings.refresh_token_expire_days),
            "invite": timedelta(days=7),
            "reset": timedelta(hours=2),
        }[token_type]

    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "scope": scope,  # "tenant" | "platform"
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    if tenant_id:
        payload["tid"] = tenant_id
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: TokenType | None = None) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except InvalidTokenError:
        return None
    if expected_type and payload.get("typ") != expected_type:
        return None
    return payload


# ── Misc helpers ──────────────────────────────────────────────────────────
def random_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def fingerprint(value: str) -> str:
    """Stable, non-reversible handle for a token (stored instead of the token)."""
    return hmac.new(settings.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def generate_license_key(slug: str) -> str:
    digest = hmac.new(settings.secret_key.encode(), slug.encode(), hashlib.sha256).hexdigest()
    chunks = [digest[i : i + 5].upper() for i in range(0, 20, 5)]
    return "SCHLY-" + "-".join(chunks)
