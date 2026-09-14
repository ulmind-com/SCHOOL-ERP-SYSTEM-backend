"""Online fee payment endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request

from app.core.config import settings
from app.core.context import AuthContext
from app.core.deps import CurrentUser, TenantDep, require
from app.models.base import AppModel
from app.modules.payments import gateway
from app.utils.audit import record

router = APIRouter(prefix="/payments/online", tags=["Online Payments"])

Viewer = Annotated[AuthContext, Depends(require("payments:read"))]


class CreateOrderRequest(AppModel):
    invoice_id: str | None = None
    student_id: str | None = None
    #: Only honoured without an invoice, and never above what is owed.
    amount: float | None = None


class ConfirmRequest(AppModel):
    order_id: str
    payment_id: str
    signature: str


@router.get("/status", summary="Whether online payment is available")
async def status(auth: CurrentUser, tenant: TenantDep):
    return {
        "enabled": settings.payments_enabled,
        "provider": "razorpay" if settings.payments_enabled else None,
        "currency": tenant.currency,
        "detail": (
            "Online payment is live."
            if settings.payments_enabled
            else "Add RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET to accept payments online."
        ),
    }


@router.post("/orders", status_code=201, summary="Start an online payment")
async def create_order(payload: CreateOrderRequest, auth: CurrentUser, tenant: TenantDep):
    """The amount is derived from the invoice on the server. A modified request
    body cannot change what gets charged."""
    return await gateway.create_order(
        tenant, auth,
        invoice_id=payload.invoice_id,
        student_id=payload.student_id,
        amount=payload.amount,
    )


@router.post("/confirm", summary="Confirm a payment from the browser callback")
async def confirm(
    payload: ConfirmRequest, auth: CurrentUser, tenant: TenantDep, request: Request
):
    result = await gateway.confirm_payment(
        tenant, auth,
        order_id=payload.order_id,
        payment_id=payload.payment_id,
        signature=payload.signature,
        source="callback",
    )
    if not result.get("already_recorded"):
        await record(auth, "payments.online_collected", entity_type="payments",
                     entity_label=result.get("receipt_number", ""),
                     changes={"amount": result.get("collected")}, request=request)
    return result


@router.post("/webhook", summary="Gateway server-to-server confirmation",
             include_in_schema=False)
async def webhook(
    request: Request,
    tenant: TenantDep,
    x_razorpay_signature: Annotated[str | None, Header()] = None,
):
    """Fires even when the payer closes the browser mid-redirect, which is the
    case the callback alone would lose."""
    body = await request.body()
    return await gateway.handle_webhook(tenant, body, x_razorpay_signature or "")


@router.get("/orders", summary="Online payment attempts")
async def orders(
    auth: Viewer,
    tenant: TenantDep,
    page: int = 1,
    page_size: Annotated[int, Query(le=100)] = 25,
    status_filter: Annotated[str, Query(alias="status")] = "",
):
    return await gateway.list_orders(
        tenant, page=page, page_size=page_size, status=status_filter
    )
