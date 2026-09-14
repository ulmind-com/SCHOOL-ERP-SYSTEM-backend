"""Online fee collection.

Razorpay is the concrete provider (UPI, cards, net banking and wallets in one
integration, which is what Indian schools need), but everything the rest of the
codebase touches goes through `PaymentGateway`, so a second provider is a new
class rather than a rewrite.

The rules that matter with money:

* The **amount is never taken from the client.** An order is created from the
  invoice the server looked up, so a tampered request cannot underpay.
* A payment is only recorded after the signature verifies against our secret.
* Capture is idempotent — Razorpay retries webhooks, and the browser callback
  and the webhook routinely both arrive.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from bson import ObjectId

from app.core.config import settings
from app.core.context import AuthContext, TenantContext
from app.core.exceptions import AppError, NotFound, ValidationError
from app.db.mongo import C, collection
from app.db.repository import Repository
from app.models.base import serialize_doc, utcnow
from app.models.finance import money

log = logging.getLogger("scholarly.payments")

RAZORPAY_API = "https://api.razorpay.com/v1"


class PaymentsDisabled(AppError):
    """Online payment is not configured for this deployment."""

    status_code = 503
    code = "payments_disabled"


@dataclass(slots=True)
class GatewayOrder:
    provider: str
    order_id: str
    amount_paise: int
    currency: str
    key_id: str


class PaymentGateway:
    """What the rest of the app is allowed to assume about a provider."""

    name = "none"

    async def create_order(self, *, amount: float, currency: str, receipt: str,
                           notes: dict[str, Any]) -> GatewayOrder:
        raise NotImplementedError

    def verify_signature(self, *, order_id: str, payment_id: str, signature: str) -> bool:
        raise NotImplementedError

    def verify_webhook(self, *, body: bytes, signature: str) -> bool:
        raise NotImplementedError

    async def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        raise NotImplementedError


class RazorpayGateway(PaymentGateway):
    name = "razorpay"

    def __init__(self) -> None:
        if not settings.payments_enabled:
            raise PaymentsDisabled(
                "Online payment is not configured. Add RAZORPAY_KEY_ID and "
                "RAZORPAY_KEY_SECRET to enable it."
            )
        token = base64.b64encode(
            f"{settings.razorpay_key_id}:{settings.razorpay_key_secret}".encode()
        ).decode()
        self._headers = {"Authorization": f"Basic {token}",
                         "Content-Type": "application/json"}

    async def create_order(self, *, amount: float, currency: str, receipt: str,
                           notes: dict[str, Any]) -> GatewayOrder:
        # Razorpay works in the smallest currency unit.
        paise = int(round(amount * 100))
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                f"{RAZORPAY_API}/orders",
                headers=self._headers,
                json={
                    "amount": paise,
                    "currency": currency,
                    "receipt": receipt[:40],
                    "notes": {k: str(v)[:200] for k, v in notes.items()},
                },
            )
        if response.status_code >= 400:
            log.error("Razorpay order failed: %s", response.text[:400])
            raise AppError("The payment could not be started. Try again in a moment.")
        body = response.json()
        return GatewayOrder(
            provider=self.name,
            order_id=body["id"],
            amount_paise=body["amount"],
            currency=body["currency"],
            key_id=settings.razorpay_key_id,
        )

    def verify_signature(self, *, order_id: str, payment_id: str, signature: str) -> bool:
        expected = hmac.new(
            settings.razorpay_key_secret.encode(),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature or "")

    def verify_webhook(self, *, body: bytes, signature: str) -> bool:
        secret = settings.razorpay_webhook_secret or settings.razorpay_key_secret
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")

    async def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.get(
                f"{RAZORPAY_API}/payments/{payment_id}", headers=self._headers
            )
        if response.status_code >= 400:
            raise AppError("Could not confirm the payment with the gateway")
        return response.json()


def get_gateway() -> PaymentGateway:
    return RazorpayGateway()


# ── Orchestration ─────────────────────────────────────────────────────────
async def create_order(
    tenant: TenantContext,
    auth: AuthContext,
    *,
    invoice_id: str | None = None,
    student_id: str | None = None,
    amount: float | None = None,
) -> dict[str, Any]:
    """Start an online payment.

    When an invoice is given, the amount is its outstanding balance — read from
    the database, never from the request — so a modified payload cannot change
    what is charged.
    """
    gateway = get_gateway()
    invoices = Repository(C.FEE_INVOICES, tenant.id, actor_id=auth.user_id)

    invoice = None
    if invoice_id:
        invoice = await invoices.get_or_404(invoice_id, label="Invoice")
        balance = money(float(invoice.get("total", 0)) - float(invoice.get("paid_amount", 0)))
        if balance <= 0:
            raise ValidationError("This invoice is already settled")
        await _assert_collection_open(tenant, invoice)
        payable = balance
        student_oid = invoice["student_id"]
    else:
        if not student_id:
            raise ValidationError("Choose an invoice or a student")
        student_oid = ObjectId(student_id)
        outstanding = await invoices.list(
            {"student_id": student_oid,
             "status": {"$in": ["issued", "partially_paid", "overdue"]}},
            sort_by="due_date", sort_dir="asc",
        )
        due = money(sum(
            float(i.get("total", 0)) - float(i.get("paid_amount", 0)) for i in outstanding
        ))
        if due <= 0:
            raise ValidationError("There is nothing outstanding for this student")
        # A part payment is allowed, but never more than what is owed.
        payable = money(min(amount, due)) if amount else due

    # A family may only pay for their own children.
    if auth.guardian_id or auth.student_id:
        allowed = await _payable_student_ids(tenant, auth)
        if student_oid not in allowed:
            from app.core.exceptions import Forbidden

            raise Forbidden("You can only pay for your own children")

    student = await collection(C.STUDENTS).find_one(
        {"_id": student_oid, "tenant_id": tenant.id}
    )
    if student is None:
        raise NotFound("Student not found")

    receipt = f"SCH-{str(student_oid)[-8:]}-{int(utcnow().timestamp())}"
    order = await gateway.create_order(
        amount=payable,
        currency=tenant.currency or "INR",
        receipt=receipt,
        notes={
            "tenant": tenant.slug,
            "student": student.get("admission_number", ""),
            "invoice": (invoice or {}).get("number", ""),
        },
    )

    await Repository(C.PAYMENT_ORDERS, tenant.id, actor_id=auth.user_id).create({
        "order_reference": order.order_id,
        "provider": order.provider,
        "student_id": student_oid,
        "invoice_id": invoice["_id"] if invoice else None,
        "invoice_number": (invoice or {}).get("number", ""),
        "amount": payable,
        "currency": order.currency,
        "status": "created",
        "initiated_by": auth.user_id,
        "receipt": receipt,
    })

    return {
        "provider": order.provider,
        "order_id": order.order_id,
        "amount": payable,
        "amount_paise": order.amount_paise,
        "currency": order.currency,
        "key_id": order.key_id,
        "student_name": " ".join(filter(None, [student.get("first_name"),
                                               student.get("last_name")])),
        "invoice_number": (invoice or {}).get("number", ""),
        "prefill": {
            "name": " ".join(filter(None, [student.get("first_name"),
                                           student.get("last_name")])),
            "email": (student.get("contact") or {}).get("email", "") or auth.email,
            "contact": (student.get("contact") or {}).get("phone", ""),
        },
    }


async def _assert_collection_open(tenant: TenantContext, invoice: dict) -> None:
    """Refuse a payment outside the window the institution set for it.

    A window enforced only in the browser is a suggestion. An examination fee
    the school closed last week has to be closed here too, or the money arrives
    and the office has to refund it.
    """
    from app.modules.fees.service import fee_plan

    plan = await fee_plan(tenant, str(invoice["student_id"]))
    for instalment in plan["instalments"]:
        if instalment.get("invoice_id") != str(invoice["_id"]):
            continue
        if instalment.get("payable"):
            return
        if instalment.get("status") == "not_open_yet":
            opens = instalment.get("opens_on") or "later"
            raise ValidationError(
                f"Payment for {instalment['period_label']} opens on {opens}."
            )
        if instalment.get("status") == "closed":
            raise ValidationError(
                f"Payment for {instalment['period_label']} closed on "
                f"{instalment.get('closes_on')}. Pay at the office instead."
            )
        return
    # An invoice the plan does not know about — raised by hand, or from a
    # structure since changed. Nothing said it was shut, so let it through.


async def _payable_student_ids(tenant: TenantContext, auth: AuthContext) -> set[ObjectId]:
    if auth.student_id:
        return {auth.student_id}
    if auth.guardian_id:
        guardian = await collection(C.GUARDIANS).find_one(
            {"_id": auth.guardian_id, "tenant_id": tenant.id}
        )
        return set((guardian or {}).get("student_ids") or [])
    return set()


async def confirm_payment(
    tenant: TenantContext,
    auth: AuthContext | None,
    *,
    order_id: str,
    payment_id: str,
    signature: str,
    source: str = "callback",
) -> dict[str, Any]:
    """Verify and bank a payment. Safe to call twice for the same payment."""
    gateway = get_gateway()
    orders = collection(C.PAYMENT_ORDERS)

    order = await orders.find_one({"tenant_id": tenant.id, "order_reference": order_id})
    if order is None:
        raise NotFound("We do not recognise that payment order")

    if order.get("status") == "paid":
        # The browser callback and the webhook both fire. The second one must
        # not raise a second receipt.
        return {
            "already_recorded": True,
            "receipt_number": order.get("receipt_number", ""),
            "detail": "This payment was already recorded",
        }

    if not gateway.verify_signature(
        order_id=order_id, payment_id=payment_id, signature=signature
    ):
        await orders.update_one(
            {"_id": order["_id"]},
            {"$set": {"status": "signature_failed", "updated_at": utcnow()}},
        )
        log.warning("Signature check failed for order %s", order_id)
        raise ValidationError("This payment could not be verified")

    remote = await gateway.fetch_payment(payment_id)
    if remote.get("status") not in {"captured", "authorized"}:
        await orders.update_one(
            {"_id": order["_id"]},
            {"$set": {"status": "failed", "gateway_status": remote.get("status"),
                      "updated_at": utcnow()}},
        )
        raise ValidationError(f"The gateway reported the payment as {remote.get('status')}")

    paid = money(float(remote.get("amount", 0)) / 100)
    method = _method_from(remote)

    from app.modules.fees.service import collect_payment

    # Reuse the counter-based collection path so an online receipt is numbered
    # in the same sequence as one taken at the desk.
    actor = auth or AuthContext(
        user_id=order.get("initiated_by") or ObjectId(),
        email="", full_name="Online payment", tenant_id=tenant.id,
    )
    result = await collect_payment(
        tenant, actor,
        student_id=str(order["student_id"]),
        amount=paid,
        method=method,
        invoice_id=str(order["invoice_id"]) if order.get("invoice_id") else None,
        reference=payment_id,
        remarks=f"Online via {order.get('provider', 'gateway')} ({source})",
    )

    await orders.update_one(
        {"_id": order["_id"]},
        {"$set": {
            "status": "paid",
            "gateway_payment_id": payment_id,
            "gateway_status": remote.get("status"),
            "method": method,
            "paid_amount": paid,
            "receipt_number": result["receipt_number"],
            "confirmed_at": utcnow(),
            "confirmed_via": source,
            "updated_at": utcnow(),
        }},
    )

    await _notify_paid(tenant, order, result, paid)
    return {**result, "already_recorded": False}


def _method_from(remote: dict[str, Any]) -> str:
    """Map the gateway's vocabulary onto ours."""
    mapping = {
        "upi": "upi",
        "card": "card",
        "netbanking": "net_banking",
        "wallet": "online",
        "emi": "card",
        "bank_transfer": "bank_transfer",
    }
    return mapping.get(str(remote.get("method", "")).lower(), "online")


async def _notify_paid(
    tenant: TenantContext, order: dict[str, Any], result: dict[str, Any], paid: float
) -> None:
    from app.modules.communication.notify import notify_users

    student = await collection(C.STUDENTS).find_one({"_id": order["student_id"]})
    if student is None:
        return
    user_ids = await collection(C.USERS).distinct("_id", {
        "tenant_id": tenant.id, "is_active": True,
        "$or": [{"student_id": student["_id"]},
                {"guardian_id": {"$in": student.get("guardian_ids") or []}}],
    })
    if not user_ids:
        return
    await notify_users(
        tenant, user_ids,
        title=f"Payment received — {result['receipt_number']}",
        body=f"We have received {tenant.currency} {paid:,.2f}. Thank you.",
        category="fee",
        type="success",
        link="/finance/invoices",
    )


async def handle_webhook(tenant: TenantContext, body: bytes, signature: str) -> dict[str, Any]:
    """Razorpay's server-to-server confirmation — the one that still arrives
    when the payer closes the browser mid-redirect."""
    gateway = get_gateway()
    if not gateway.verify_webhook(body=body, signature=signature):
        raise ValidationError("Webhook signature did not verify")

    import json

    event = json.loads(body.decode("utf-8"))
    kind = event.get("event", "")
    if kind not in {"payment.captured", "payment.authorized"}:
        return {"ignored": kind}

    entity = (
        event.get("payload", {}).get("payment", {}).get("entity", {})
    )
    order_id = entity.get("order_id")
    payment_id = entity.get("id")
    if not order_id or not payment_id:
        return {"ignored": "missing ids"}

    # The webhook body is already signed as a whole, so the per-payment
    # signature is synthesised from our own secret to reuse one code path.
    synthetic = hmac.new(
        settings.razorpay_key_secret.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()

    return await confirm_payment(
        tenant, None, order_id=order_id, payment_id=payment_id,
        signature=synthetic, source="webhook",
    )


async def list_orders(
    tenant: TenantContext, *, page: int = 1, page_size: int = 25, status: str = ""
) -> dict[str, Any]:
    query: dict[str, Any] = {"tenant_id": tenant.id, "is_deleted": {"$ne": True}}
    if status:
        query["status"] = status

    orders = collection(C.PAYMENT_ORDERS)
    total = await orders.count_documents(query)
    docs = await orders.find(query).sort([("created_at", -1)]).skip(
        (page - 1) * page_size
    ).limit(page_size).to_list(length=page_size)

    total_pages = max(1, -(-total // page_size))
    return {
        "items": [serialize_doc(d) for d in docs],
        "meta": {"page": page, "page_size": page_size, "total": total,
                 "total_pages": total_pages, "has_next": page < total_pages,
                 "has_prev": page > 1},
    }
