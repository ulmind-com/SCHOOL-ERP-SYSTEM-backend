"""Application configuration.

One codebase, two deployment shapes:

* ``saas``       – many institutions share this deployment and its database.
                   Tenant is resolved per request; plan limits and subscription
                   state are enforced; the platform console is mounted.
* ``dedicated``  – a single institution owns this deployment outright. It has
                   its own database and its own ImageKit folder, the platform
                   console is not mounted, and nothing is gated behind a plan.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class DeploymentMode(StrEnum):
    SAAS = "saas"
    DEDICATED = "dedicated"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Deployment ────────────────────────────────────────────────────────
    deployment_mode: DeploymentMode = DeploymentMode.SAAS
    dedicated_tenant_slug: str = ""
    dedicated_tenant_name: str = ""
    dedicated_license_key: str = ""

    # ── App ───────────────────────────────────────────────────────────────
    app_name: str = "Scholarly"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    backend_port: int = 8000

    # ── Security ──────────────────────────────────────────────────────────
    secret_key: str = "insecure-dev-key-change-me"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30
    password_min_length: int = 8
    jwt_algorithm: str = "HS256"

    # ── Database ──────────────────────────────────────────────────────────
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "scholarly"

    # ── CORS / routing ────────────────────────────────────────────────────
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    tenant_base_domain: str = ""
    #: Where the browser app lives. Used to build the links inside emails —
    #: the API has no way to guess it, and a reset mail with a link to the API
    #: is useless to the person reading it.
    web_app_url: str = "http://localhost:3000"

    # ── ImageKit ──────────────────────────────────────────────────────────
    imagekit_public_key: str = ""
    imagekit_private_key: str = ""
    imagekit_url_endpoint: str = ""
    imagekit_id: str = ""

    # ── Email (SMTP) ──────────────────────────────────────────────────────
    #: Resend is tried before SMTP — it is an HTTPS call rather than a socket,
    #: which is the difference between working and not on a PaaS that blocks
    #: outbound port 587.
    resend_api_key: str = ""
    mail_address: str = ""
    mail_from_name: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True

    # ── SMS (generic HTTP gateway: MSG91, TextLocal, Fast2SMS, Gupshup…) ───
    sms_api_url: str = ""
    sms_api_key: str = ""
    sms_sender_id: str = ""
    sms_auth_header: str = ""           # e.g. "authkey"; blank sends apikey in the body
    sms_field_to: str = "mobiles"
    sms_field_message: str = "message"
    sms_field_sender: str = "sender"

    # ── Push (Firebase Cloud Messaging) ───────────────────────────────────
    fcm_server_key: str = ""

    # ── WhatsApp (Meta Cloud API) ─────────────────────────────────────────
    whatsapp_api_url: str = ""
    whatsapp_token: str = ""

    # ── Payments (Razorpay) ───────────────────────────────────────────────
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # ── AI assistant (Anthropic) ──────────────────────────────────────────
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # ── Biometric devices ─────────────────────────────────────────────────
    # Shared secret a ZKTeco/ADMS device must present. Blank disables the
    # endpoint entirely rather than leaving it open.
    biometric_device_key: str = ""

    # ── Bootstrap ─────────────────────────────────────────────────────────
    #: Seed plans and the platform owner on startup instead of via a shell.
    #: Render's free tier has no SSH and no one-off jobs, so there is nowhere to
    #: run the seed script — this is the way in. The bootstrap is idempotent, so
    #: leaving it on across deploys is harmless.
    bootstrap_on_startup: bool = False
    platform_owner_email: str = "owner@example.com"
    platform_owner_password: str = "ChangeMe123!"
    platform_owner_name: str = "Platform Owner"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _check_dedicated(self) -> Settings:
        if self.deployment_mode is DeploymentMode.DEDICATED and not self.dedicated_tenant_slug:
            raise ValueError(
                "DEDICATED_TENANT_SLUG is required when DEPLOYMENT_MODE=dedicated"
            )
        return self

    # ── Convenience ───────────────────────────────────────────────────────
    @property
    def is_saas(self) -> bool:
        return self.deployment_mode is DeploymentMode.SAAS

    @property
    def is_dedicated(self) -> bool:
        return self.deployment_mode is DeploymentMode.DEDICATED

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def imagekit_enabled(self) -> bool:
        return bool(self.imagekit_private_key and self.imagekit_url_endpoint)

    @property
    def email_enabled(self) -> bool:
        return bool(self.resend_api_key and self.mail_address) or bool(self.smtp_host)

    @property
    def mail_sender(self) -> str:
        """``Name <address>`` when a display name is set, else the bare address."""
        address = self.mail_address or self.smtp_from or self.smtp_user
        name = self.mail_from_name or self.app_name
        return f"{name} <{address}>" if address and name else address

    @property
    def payments_enabled(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def biometrics_enabled(self) -> bool:
        return bool(self.biometric_device_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
