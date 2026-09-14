"""Typed application errors mapped to clean JSON responses."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


class AppError(HTTPException):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"

    def __init__(self, detail: str | None = None, *, meta: dict[str, Any] | None = None):
        super().__init__(status_code=self.status_code, detail=detail or self.__doc__ or self.code)
        self.meta = meta or {}


class NotFound(AppError):
    """The requested resource does not exist."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class Conflict(AppError):
    """That value is already taken."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class Unauthorized(AppError):
    """Authentication is required."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class Forbidden(AppError):
    """You do not have access to this."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class ValidationError(AppError):
    """The submitted data is invalid."""

    status_code = 422
    code = "validation_error"


class TenantError(AppError):
    """The institution could not be resolved."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "tenant_error"


class SubscriptionError(AppError):
    """This institution's subscription does not allow that."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "subscription_required"


class ModuleDisabled(AppError):
    """This module is switched off for your institution."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "module_disabled"


class LimitExceeded(AppError):
    """A plan limit has been reached."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "limit_exceeded"
