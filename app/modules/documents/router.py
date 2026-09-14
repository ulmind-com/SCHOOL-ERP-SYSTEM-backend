"""Uploads and the institution's document vault."""

from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.core.context import AuthContext
from app.core.crud import Resource, build_crud_router
from app.core.deps import CurrentUser, TenantDep, require
from app.core.exceptions import NotFound
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models import communication as comm
from app.models.base import Msg, serialize_doc, utcnow
from app.modules.documents import imagekit
from app.utils.audit import record

router = APIRouter()

DOCUMENTS = Resource(
    name="documents", collection=C.DOCUMENTS, module="documents", model=comm.Document,
    tags=["Documents"], search_fields=["name", "description", "tags"],
    filters=["category", "owner_type", "owner_id", "is_private"],
    sortable=["created_at", "name"],
)
router.include_router(build_crud_router(DOCUMENTS))

files = APIRouter(prefix="/files", tags=["Files"])

Uploader = Annotated[AuthContext, Depends(require("documents:create"))]


@files.get("/upload-auth", summary="Credentials for a direct browser upload")
async def upload_auth(auth: CurrentUser, tenant: TenantDep, category: str = "general"):
    """Hands the browser a short-lived signature so big files go straight to
    ImageKit instead of through this API."""
    return imagekit.signed_upload_params(tenant, category)


@files.post("/upload", summary="Upload a file through the API")
async def upload(
    auth: Uploader,
    tenant: TenantDep,
    request: Request,
    file: Annotated[UploadFile, File()],
    category: Annotated[str, Form()] = "general",
    owner_type: Annotated[str, Form()] = "institution",
    owner_id: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    save_to_vault: Annotated[bool, Form()] = True,
):
    content = await file.read()
    stored = await imagekit.upload(
        content=content,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        tenant=tenant,
        category=category,
    )

    document_id = None
    if save_to_vault:
        doc = await Repository(C.DOCUMENTS, tenant.id, actor_id=auth.user_id).create({
            "name": name or stored["name"],
            "category": category,
            "owner_type": owner_type,
            "owner_id": ObjectId(owner_id) if ObjectId.is_valid(owner_id) else None,
            "file": stored,
            "uploaded_by": auth.user_id,
            "is_private": True,
        })
        document_id = str(doc["_id"])

    # Storage usage feeds the plan's storage ceiling.
    await collection(C.TENANTS).update_one(
        {"_id": tenant.id},
        {"$inc": {"storage_used_mb": round(len(content) / 1024 / 1024, 4)}},
    )
    await record(auth, "files.upload", entity_type="documents", entity_id=document_id,
                 entity_label=stored["name"], request=request)
    return {"file": stored, "document_id": document_id, "detail": "File uploaded"}


@files.delete("/{file_id}", response_model=Msg, summary="Delete a stored file")
async def delete_file(
    file_id: str,
    auth: Annotated[AuthContext, Depends(require("documents:delete"))],
    tenant: TenantDep,
    request: Request,
):
    doc = await collection(C.DOCUMENTS).find_one(
        {"tenant_id": tenant.id, "file.file_id": file_id, "is_deleted": {"$ne": True}}
    )
    removed = await imagekit.delete(file_id)
    if doc:
        await collection(C.DOCUMENTS).update_one(
            {"_id": doc["_id"]},
            {"$set": {"is_deleted": True, "deleted_at": utcnow(), "updated_at": utcnow()}},
        )
        size_mb = round(float((doc.get("file") or {}).get("size", 0)) / 1024 / 1024, 4)
        await collection(C.TENANTS).update_one(
            {"_id": tenant.id}, {"$inc": {"storage_used_mb": -size_mb}}
        )
    elif not removed:
        raise NotFound("File not found")
    await record(auth, "files.delete", entity_type="documents", entity_label=file_id,
                 request=request)
    return Msg(detail="File deleted")


@files.get("/for/{owner_type}/{owner_id}", summary="Files attached to a record")
async def files_for(owner_type: str, owner_id: str, auth: CurrentUser, tenant: TenantDep):
    docs = await collection(C.DOCUMENTS).find({
        "tenant_id": tenant.id, "owner_type": owner_type,
        "owner_id": ObjectId(owner_id) if ObjectId.is_valid(owner_id) else owner_id,
        "is_deleted": {"$ne": True},
    }).sort([("created_at", -1)]).to_list(length=200)
    return [serialize_doc(d) for d in docs]


router.include_router(files)
