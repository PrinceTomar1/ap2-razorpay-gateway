"""Razorpay webhooks, and the polling fallback for when you have no public URL.

A webhook is an unauthenticated POST from the internet claiming a payment
happened. The only thing that makes it trustworthy is the HMAC-SHA256 signature
in ``X-Razorpay-Signature``, computed over the **raw request body** with the
webhook secret. So:

* We verify the signature before parsing anything. Not after, not "if the secret
  is configured" — before. An unsigned or wrongly signed webhook is logged and
  dropped, and the drop is itself an audit row, because a stream of rejected
  webhooks is something an operator wants to see.
* We verify against the **raw bytes**. Re-serialising the parsed JSON and hashing
  that would compute a signature over a different byte string than Razorpay
  signed, and would either always fail or — worse, if it happened to match —
  create a parser-differential hole.
* A verified webhook is still only *information*. It resolves a pending payment;
  it can never authorise one. Authorisation happens in gateway/verify.py, before
  the order exists.

**When you have no public URL** (the common case on a laptop), you do not need
webhooks at all. The recovery path already polls ``order.payments`` — see
:meth:`gateway.razorpay_client.RazorpayRail.complete_test_payment` — and that is
the same source of truth Razorpay uses to build the webhook. Webhooks are faster;
polling is sufficient. docs/RAZORPAY_TESTING.md covers both, including the ngrok
setup and the test-mode OTP.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ap2_min.roles import ROLE_MPP
from gateway.audit import AuditLog, Event

#: Events we act on. Anything else is recorded and ignored — a webhook endpoint
#: that tries to handle every event Razorpay might ever add is an endpoint that
#: will one day do something surprising.
HANDLED_EVENTS = frozenset(
    {"payment.captured", "payment.failed", "payment_link.paid", "order.paid"}
)


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 over the raw body, compared in constant time.

    This is Razorpay's documented scheme and is what
    ``razorpay.Utility.verify_webhook_signature`` does. Implemented here directly
    so the check does not depend on the SDK being importable, and so the
    comparison is visibly ``compare_digest`` rather than ``==``.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class WebhookReceiver:
    """Resolves pending payments from verified Razorpay callbacks.

    Replay protection
    -----------------
    A valid signature proves a webhook *came from Razorpay*. It does not prove it
    has not been delivered before. Razorpay retries on any non-2xx, so duplicates
    are normal rather than exceptional, and anybody who captures one body can
    replay it verbatim for as long as the secret lives.

    So every delivery is keyed on ``X-Razorpay-Event-Id`` and answered exactly
    once. A repeat returns 200 with ``duplicate: true`` — 200 because the delivery
    genuinely succeeded and Razorpay should stop retrying, and ``duplicate``
    because an operator watching the audit trail should be able to tell a retry
    storm from real traffic.

    A delivery with no event id is still processed (older Razorpay integrations
    omit it) but is recorded as un-deduplicable, so the gap is visible rather than
    silent.
    """

    def __init__(self, *, audit: AuditLog, secret: str | None = None) -> None:
        self.audit = audit
        self.secret = (
            secret if secret is not None else os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
        )
        #: Event ids already answered. In a multi-process deployment this belongs
        #: in the database next to the idempotency store; it is in memory here
        #: because the gateway is a single process, and that is recorded in
        #: LIMITATIONS.md rather than glossed over.
        self._seen_event_ids: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self.secret)

    def handle(self, raw_body: bytes, signature: str, event_id: str = "") -> dict[str, Any]:
        """Verify, then deduplicate, then record. Returns what the endpoint replies.

        Raises :class:`fastapi.HTTPException` 400 on a bad signature, which is the
        correct answer: we are telling Razorpay we did not accept the delivery, so
        it will retry, rather than silently swallowing it with a 200.
        """
        if not verify_webhook_signature(raw_body, signature, self.secret):
            self.audit.append(
                ROLE_MPP,
                Event.WEBHOOK_REJECTED,
                {"bytes": len(raw_body), "had_signature": bool(signature)},
                "A webhook arrived without a valid Razorpay signature and was dropped. "
                "Nothing it claimed was acted on.",
            )
            raise HTTPException(status_code=400, detail="invalid webhook signature")

        import json

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="webhook body is not JSON") from exc

        event = str(body.get("event", "unknown"))

        # Signature first, deduplication second. Checking the id before the
        # signature would let an unauthenticated caller poison the seen-set and
        # suppress a genuine webhook.
        if event_id and event_id in self._seen_event_ids:
            self.audit.append(
                ROLE_MPP,
                Event.WEBHOOK_REPLAYED,
                {"event": event, "event_id": event_id},
                f"Razorpay re-delivered event {event_id} ({event}). It was already "
                "answered, so it was acknowledged and ignored.",
            )
            return {"received": True, "event": event, "handled": False, "duplicate": True}
        if event_id:
            self._seen_event_ids.add(event_id)

        entity = _first_entity(body)
        payload = {
            "event": event,
            "handled": event in HANDLED_EVENTS,
            "payment_id": entity.get("id"),
            "order_id": entity.get("order_id"),
            "amount": entity.get("amount"),
            "status": entity.get("status"),
            "method": entity.get("method"),
            "event_id": event_id or None,
            "deduplicable": bool(event_id),
        }
        self.audit.append(
            ROLE_MPP,
            Event.WEBHOOK_RECEIVED,
            payload,
            (
                f"Razorpay reported {event} for order {payload['order_id']}. "
                "Recorded as information — a webhook resolves a payment, it never authorises one."
            ),
        )
        return {
            "received": True,
            "event": event,
            "handled": payload["handled"],
            "duplicate": False,
        }


def _first_entity(body: dict[str, Any]) -> dict[str, Any]:
    """Dig the payment/order entity out of Razorpay's nested webhook envelope.

    Shape is ``{"payload": {"payment": {"entity": {...}}}}``. Defensive because
    this is untrusted input whose shape we do not control.
    """
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return {}
    for key in ("payment", "order", "payment_link"):
        wrapper = payload.get(key)
        if isinstance(wrapper, dict):
            entity = wrapper.get("entity")
            if isinstance(entity, dict):
                return entity
    return {}


def build_router(receiver: WebhookReceiver) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    @router.post("/razorpay")
    async def razorpay_webhook(
        request: Request,
        x_razorpay_signature: str = Header(default=""),
        x_razorpay_event_id: str = Header(default=""),
    ) -> JSONResponse:
        """Receive a Razorpay webhook. Signature first, deduplication second."""
        raw = await request.body()
        return JSONResponse(receiver.handle(raw, x_razorpay_signature, x_razorpay_event_id))

    @router.get("/razorpay/health")
    async def webhook_health() -> JSONResponse:
        return JSONResponse(
            {
                "configured": receiver.configured,
                "note": (
                    "Set RAZORPAY_WEBHOOK_SECRET to accept webhooks. Without it the gateway "
                    "polls order.payments instead, which is the same source of truth."
                ),
            }
        )

    return router
