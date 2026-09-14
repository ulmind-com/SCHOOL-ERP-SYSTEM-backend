"""Library circulation endpoints."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.context import AuthContext
from app.core.deps import TenantDep, require
from app.models.base import AppModel
from app.modules.library import service
from app.utils.audit import record

router = APIRouter(prefix="/library", tags=["Library"])

Reader = Annotated[AuthContext, Depends(require("library:read"))]
Librarian = Annotated[AuthContext, Depends(require("library:update"))]


class IssueRequest(AppModel):
    item_id: str
    borrower_type: str = "student"
    borrower_id: str
    due_date: date | None = None


class ReturnRequest(AppModel):
    condition: str = "good"        # good | damaged | lost
    waive_fine: bool = False


@router.get("/dashboard", summary="Circulation at a glance")
async def dashboard(auth: Librarian, tenant: TenantDep):
    return await service.dashboard(tenant)


@router.get("/loans", summary="Loans")
async def loans(
    auth: Reader,
    tenant: TenantDep,
    page: int = 1,
    page_size: Annotated[int, Query(le=200)] = 25,
    status: str = "",
    borrower_id: str = "",
):
    from bson import ObjectId

    from app.core.scoping import family_student_ids
    from app.db.mongo import C
    from app.db.repository import Repository

    repo = Repository(C.LIBRARY_LOANS, tenant.id)
    query: dict = {}
    if status:
        query["status"] = status
    if borrower_id and ObjectId.is_valid(borrower_id):
        query["borrower_id"] = ObjectId(borrower_id)

    # library:read lets a student look up the catalogue. It is not a reason to
    # show them who else has borrowed what, or what they owe in fines.
    allowed = await family_student_ids(auth, tenant)
    if allowed is not None:
        query["borrower_id"] = {"$in": allowed}

    return await repo.paginate(query, page=page, page_size=page_size,
                               sort_by="issued_on", sort_dir="desc")


@router.post("/issue", status_code=201, summary="Issue an item")
async def issue(payload: IssueRequest, auth: Librarian, tenant: TenantDep, request: Request):
    result = await service.issue(
        tenant, auth,
        item_id=payload.item_id,
        borrower_type=payload.borrower_type,
        borrower_id=payload.borrower_id,
        due_date=payload.due_date,
    )
    await record(auth, "library.issued", entity_type="library_loan",
                 entity_id=result.get("id"), entity_label=result.get("item_title", ""),
                 request=request)
    return result


@router.post("/loans/{loan_id}/return", summary="Take an item back")
async def return_item(
    loan_id: str, payload: ReturnRequest, auth: Librarian, tenant: TenantDep, request: Request
):
    result = await service.return_item(
        tenant, auth, loan_id, condition=payload.condition, waive_fine=payload.waive_fine
    )
    await record(auth, "library.returned", entity_type="library_loan", entity_id=loan_id,
                 changes={"fine": result.get("fine"), "condition": payload.condition},
                 request=request)
    return result


@router.post("/loans/{loan_id}/renew", summary="Renew a loan")
async def renew(loan_id: str, auth: Librarian, tenant: TenantDep, request: Request):
    result = await service.renew(tenant, auth, loan_id)
    await record(auth, "library.renewed", entity_type="library_loan", entity_id=loan_id,
                 request=request)
    return result


@router.post("/mark-overdue", summary="Flag overdue loans and notify borrowers")
async def mark_overdue(auth: Librarian, tenant: TenantDep, request: Request):
    result = await service.mark_overdue(tenant)
    await record(auth, "library.mark_overdue", entity_type="library_loan",
                 changes=result, request=request)
    return result


@router.get("/borrowers/{borrower_id}", summary="A borrower's history")
async def borrower(borrower_id: str, auth: Reader, tenant: TenantDep):
    from app.core.scoping import assert_may_see_student

    await assert_may_see_student(auth, tenant, borrower_id)
    return await service.borrower_history(tenant, borrower_id)
