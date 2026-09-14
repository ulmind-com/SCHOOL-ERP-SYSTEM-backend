"""Data export.

An institution's answer to "can we get our data out?" should be a button, not a
support ticket — especially for a school weighing a dedicated licence. This
exports every collection the institution owns as one JSON file, in exactly the
shape the database holds, so it can be loaded into their own deployment
verbatim.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from typing import Any

from bson import ObjectId

from app.core.context import TenantContext
from app.db.mongo import C, collection, get_database
from app.models.base import utcnow

#: Never leave the institution: platform-side commercial records, and anything
#: that would let an export be replayed as a login.
EXCLUDED = {
    C.TENANTS, C.PLANS, C.SUBSCRIPTIONS, C.PLATFORM_USERS,
    C.PLATFORM_INVOICES, C.PLATFORM_AUDIT, C.SESSIONS,
}

#: Stripped from every document before it is written out.
REDACTED_FIELDS = {
    "password_hash", "invite_token_hash", "reset_token_hash", "device_tokens",
}


def _encode(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return {"$oid": str(value)}
    if isinstance(value, datetime):
        return {"$date": value.isoformat()}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items() if k not in REDACTED_FIELDS}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    return value


async def export_tenant(tenant: TenantContext) -> tuple[bytes, str, dict[str, int]]:
    """Every document this institution owns, as a zip of JSON files.

    Extended JSON (`{"$oid": ...}`) is used deliberately: `mongoimport` reads it
    natively, so restoring is a command rather than a script.
    """
    database = get_database()
    names = [n for n in await database.list_collection_names() if n not in EXCLUDED]

    buffer = io.BytesIO()
    counts: dict[str, int] = {}

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            cursor = collection(name).find({"tenant_id": tenant.id})
            documents = await cursor.to_list(length=None)
            if not documents:
                continue
            counts[name] = len(documents)
            # One JSON array per collection, matching mongoimport --jsonArray.
            archive.writestr(
                f"{name}.json",
                json.dumps([_encode(d) for d in documents], indent=1, ensure_ascii=False),
            )

        archive.writestr("MANIFEST.json", json.dumps({
            "institution": tenant.name,
            "slug": tenant.slug,
            "tenant_id": str(tenant.id),
            "deployment": tenant.deployment,
            "exported_at": utcnow().isoformat(),
            "collections": counts,
            "documents": sum(counts.values()),
            "format": "MongoDB Extended JSON (relaxed), one array per collection",
            "redacted": sorted(REDACTED_FIELDS),
            "restore": (
                "mongoimport --uri <your-uri> --collection <name> "
                "--jsonArray --file <name>.json"
            ),
            "note": (
                "Every document keeps its original _id and tenant_id, so this "
                "loads into a dedicated deployment unchanged."
            ),
        }, indent=2))

    filename = f"{tenant.slug}-export-{utcnow():%Y%m%d-%H%M}.zip"
    return buffer.getvalue(), filename, counts


async def export_summary(tenant: TenantContext) -> dict[str, Any]:
    """What an export would contain, without building it."""
    database = get_database()
    names = [n for n in await database.list_collection_names() if n not in EXCLUDED]
    counts: dict[str, int] = {}
    for name in sorted(names):
        total = await collection(name).count_documents({"tenant_id": tenant.id})
        if total:
            counts[name] = total
    return {
        "collections": len(counts),
        "documents": sum(counts.values()),
        "breakdown": counts,
        "excluded": sorted(EXCLUDED),
        "redacted_fields": sorted(REDACTED_FIELDS),
    }
