"""ImageKit storage.

Talks to the REST API over httpx rather than the vendor SDK: fewer moving
parts, and the signature helper below is what lets a browser upload straight to
ImageKit without the file ever passing through this server.

Every institution gets its own folder, so a dedicated school's media can be
handed over — or deleted — as one unit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.context import TenantContext
from app.core.exceptions import AppError, ValidationError
from app.core.security import random_token

log = logging.getLogger("scholarly.imagekit")

UPLOAD_URL = "https://upload.imagekit.io/api/v1/files/upload"
API_BASE = "https://api.imagekit.io/v1"

MAX_UPLOAD_MB = 25
ALLOWED_MIME_PREFIXES = ("image/", "video/", "audio/")
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain", "text/csv", "application/zip",
}


class StorageDisabled(AppError):
    """File storage is not configured for this deployment."""

    status_code = 503
    code = "storage_disabled"


def _auth_header() -> dict[str, str]:
    token = base64.b64encode(f"{settings.imagekit_private_key}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def require_enabled() -> None:
    if not settings.imagekit_enabled:
        raise StorageDisabled(
            "File storage is not configured. Add your ImageKit keys to the environment."
        )


def check_upload(filename: str, content_type: str, size: int) -> None:
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f"Files must be {MAX_UPLOAD_MB}MB or smaller")
    if not size:
        raise ValidationError("That file is empty")
    allowed = content_type in ALLOWED_MIME_TYPES or content_type.startswith(
        ALLOWED_MIME_PREFIXES
    )
    if not allowed:
        raise ValidationError(f"'{content_type}' files are not accepted")


def folder_for(tenant: TenantContext, category: str = "general") -> str:
    return f"{tenant.storage_path()}/{category.strip('/') or 'general'}"


def safe_name(filename: str) -> str:
    stem = "".join(c if c.isalnum() or c in "-_." else "-" for c in filename)[-120:]
    return f"{int(time.time())}-{random_token(4)[:6]}-{stem}"


async def upload(
    *,
    content: bytes,
    filename: str,
    content_type: str,
    tenant: TenantContext,
    category: str = "general",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    require_enabled()
    check_upload(filename, content_type, len(content))

    data = {
        "fileName": safe_name(filename),
        "folder": folder_for(tenant, category),
        "useUniqueFileName": "true",
        "tags": ",".join([tenant.slug, category, *(tags or [])]),
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            UPLOAD_URL,
            headers=_auth_header(),
            data=data,
            files={"file": (filename, content, content_type)},
        )
    if response.status_code >= 400:
        log.error("ImageKit upload failed (%s): %s", response.status_code, response.text[:400])
        raise AppError("The file could not be stored. Try again in a moment.")

    body = response.json()
    return {
        "file_id": body.get("fileId", ""),
        "name": body.get("name", filename),
        "url": body.get("url", ""),
        "thumbnail_url": body.get("thumbnailUrl", ""),
        "file_path": body.get("filePath", ""),
        "size": int(body.get("size", len(content))),
        "mime_type": body.get("fileType") or content_type,
        "width": body.get("width"),
        "height": body.get("height"),
    }


async def delete(file_id: str) -> bool:
    require_enabled()
    if not file_id:
        return False
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(f"{API_BASE}/files/{file_id}", headers=_auth_header())
    if response.status_code in (200, 204):
        return True
    log.warning("ImageKit delete failed for %s: %s", file_id, response.text[:200])
    return False


def signed_upload_params(tenant: TenantContext, category: str = "general") -> dict[str, Any]:
    """Credentials for a direct browser → ImageKit upload.

    The private key never leaves the server; the browser gets a token, an expiry
    and an HMAC over the two. Large photo and document uploads then skip this
    service entirely instead of tying up a worker.
    """
    require_enabled()
    token = random_token(16)
    expire = int(time.time()) + 20 * 60  # ImageKit caps this at ~1 hour
    signature = hmac.new(
        settings.imagekit_private_key.encode(),
        f"{token}{expire}".encode(),
        hashlib.sha1,
    ).hexdigest()
    return {
        "token": token,
        "expire": expire,
        "signature": signature,
        "public_key": settings.imagekit_public_key,
        "url_endpoint": settings.imagekit_url_endpoint,
        "folder": folder_for(tenant, category),
        "upload_url": UPLOAD_URL,
        "max_size_mb": MAX_UPLOAD_MB,
    }


def transformed(url: str, *, width: int | None = None, height: int | None = None,
                quality: int = 80, crop: str = "maintain_ratio") -> str:
    """Ask ImageKit for a resized variant instead of shipping the original."""
    if not url:
        return ""
    parts = [f"q-{quality}"]
    if width:
        parts.append(f"w-{width}")
    if height:
        parts.append(f"h-{height}")
    if width and height:
        parts.append(f"c-{crop}")
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}tr={','.join(parts)}"
