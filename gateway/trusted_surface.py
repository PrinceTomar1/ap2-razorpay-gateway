"""The Trusted Surface: the human gate. Deliberately, structurally non-agentic.

    "The Trusted Surface role is a UI surface that is trusted to get informed
    user consent for an Intent before creating a user-signed Mandate."
        — AP2 v0.2, docs/ap2/specification.md

This is the one surface in the whole system where a person's judgement is the
input, so it is the one surface where a language model would do the most damage.
There is no model here, no import path to ``llm/``, and no generated text: the
page is a fixed template with the *verifier's own* deterministic sentence and the
*merchant's own signed* numbers substituted in. A user asked to approve ₹4,999
sees ₹4,999 because that integer came out of a signed Checkout Mandate, not
because something summarised it.

What approval produces
----------------------
Not "unlock the limit". Approval mints the **narrowest mandate that can possibly
authorise this one purchase**:

* ``payment.amount_range`` with ``min == max == this exact amount``
* ``payment.budget`` equal to that same amount, so it can fund one payment and
  never a second
* ``payment.allowed_payees`` containing only this merchant
* ``payment.reference`` pinned to the hash of this specific Checkout Mandate
* an expiry measured in minutes

So a user who approves one ₹4,999 pair of shoes has authorised exactly that, at
exactly that shop, for exactly the next few minutes — not raised their standing
limit. Getting this wrong is the difference between a consent gate and a
permission escalation.

Alongside it, the surface signs a user **Checkout** Mandate over the same cart,
which is AP2's human-present cart confirmation: the buyer's own signature saying
"yes, this basket, at this price".

Denial produces nothing at all, which is the correct output. There is no
half-approved state.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from ap2_min.builders import closed_checkout_mandate, closed_payment_mandate, open_payment_mandate
from ap2_min.models import Cart, paise_to_inr_str
from ap2_min.roles import ROLE_TRUSTED_SURFACE
from gateway.audit import AuditLog, Event
from gateway.mandates import Signer, checkout_hash, new_id, utcnow

#: How long a held request waits for a human before it lapses. Short: an approval
#: prompt a user answers tomorrow is not informed consent about today's cart, and
#: the merchant's price guarantee will have expired anyway.
HOLD_TTL_SECONDS = 900

#: Lifetime of the one-time mandate an approval mints. Shorter still — it exists
#: only to carry this one payment from the approval click to the payment rail.
ONE_TIME_MANDATE_TTL_SECONDS = 600

GateStatus = Literal["pending", "approved", "denied", "expired"]


@dataclass
class HeldRequest:
    """One purchase waiting on a person."""

    hold_id: str
    checkout_id: str
    checkout_jws: str
    cart: Cart
    amount: int
    constraint_code: str
    human_reason: str
    created_ts: str = field(default_factory=lambda: utcnow().isoformat())
    status: GateStatus = "pending"
    decided_ts: str | None = None
    checkout_mandate_jws: str | None = None
    payment_mandate_jws: str | None = None
    payment_mandate_id: str | None = None

    def is_expired(self, *, now_iso: str | None = None) -> bool:
        from datetime import datetime

        now = datetime.fromisoformat(now_iso) if now_iso else utcnow()
        created = datetime.fromisoformat(self.created_ts)
        return (now - created).total_seconds() > HOLD_TTL_SECONDS

    def as_dict(self) -> dict[str, Any]:
        return {
            "hold_id": self.hold_id,
            "checkout_id": self.checkout_id,
            "status": self.status,
            "amount": self.amount,
            "amount_inr": paise_to_inr_str(self.amount),
            "merchant": self.cart.merchant_name,
            "constraint": self.constraint_code,
            "human_reason": self.human_reason,
            "created_ts": self.created_ts,
            "decided_ts": self.decided_ts,
            "checkout_mandate_jws": self.checkout_mandate_jws,
            "payment_mandate_jws": self.payment_mandate_jws,
        }


class TrustedSurface:
    """Holds requests, renders them, and signs the user's answer.

    Holds live in memory. They are short-lived by design (see
    :data:`HOLD_TTL_SECONDS`) and losing an undecided one on a restart is the
    correct failure: it means nobody was asked, so nobody consented, so nothing
    is authorised.
    """

    def __init__(
        self,
        *,
        user_signer: Signer,
        audit: AuditLog,
        public_url: str = "http://127.0.0.1:8000",
    ) -> None:
        self.user_signer = user_signer
        self.audit = audit
        self.public_url = public_url.rstrip("/")
        self._holds: dict[str, HeldRequest] = {}

    # -- lifecycle ----------------------------------------------------------

    def hold(
        self,
        *,
        checkout_id: str,
        checkout_jws: str,
        cart: Cart,
        constraint_code: str,
        human_reason: str,
    ) -> HeldRequest:
        """Park a purchase and ask a human. Returns the held request."""
        request = HeldRequest(
            hold_id=new_id("gate"),
            checkout_id=checkout_id,
            checkout_jws=checkout_jws,
            cart=cart,
            amount=cart.total,
            constraint_code=constraint_code,
            human_reason=human_reason,
        )
        self._holds[request.hold_id] = request
        self.audit.append(
            ROLE_TRUSTED_SURFACE,
            Event.GATE_REQUESTED,
            {
                "hold_id": request.hold_id,
                "checkout_id": checkout_id,
                "amount": cart.total,
                "merchant": cart.merchant_name,
                "constraint": constraint_code,
                "url": self.url_for(request.hold_id),
            },
            f"Held ₹{paise_to_inr_str(cart.total)} at {cart.merchant_name} for the buyer to "
            f"decide: {human_reason}",
        )
        return request

    def get(self, hold_id: str) -> HeldRequest:
        try:
            request = self._holds[hold_id]
        except KeyError:
            raise KeyError(f"no held request {hold_id!r}") from None
        if request.status == "pending" and request.is_expired():
            request.status = "expired"
            self.audit.append(
                ROLE_TRUSTED_SURFACE,
                Event.GATE_EXPIRED,
                {"hold_id": hold_id, "amount": request.amount},
                f"The request to approve ₹{paise_to_inr_str(request.amount)} lapsed after "
                f"{HOLD_TTL_SECONDS // 60} minutes with no answer. Nothing was charged.",
            )
        return request

    def pending(self) -> list[HeldRequest]:
        return [h for h in self._holds.values() if h.status == "pending"]

    def url_for(self, hold_id: str) -> str:
        return f"{self.public_url}/trusted-surface/{hold_id}"

    # -- the decision -------------------------------------------------------

    def decide(self, hold_id: str, *, approve: bool) -> HeldRequest:
        """Record the human's answer, and on approval mint the one-time mandates.

        This method is the whole security boundary of the gate: it is the only
        code in the system that signs with the user's key on the strength of a
        person having looked at something.
        """
        request = self.get(hold_id)
        if request.status != "pending":
            # Idempotent by nature: a double-clicked Approve must not mint a
            # second mandate, and a Deny after an Approve must not un-authorise
            # something already in flight.
            return request

        request.decided_ts = utcnow().isoformat()

        if not approve:
            request.status = "denied"
            self.audit.append(
                ROLE_TRUSTED_SURFACE,
                Event.GATE_DENIED,
                {
                    "hold_id": hold_id,
                    "checkout_id": request.checkout_id,
                    "amount": request.amount,
                    "merchant": request.cart.merchant_name,
                },
                f"The buyer declined ₹{paise_to_inr_str(request.amount)} at "
                f"{request.cart.merchant_name}. Nothing was charged and no mandate was issued.",
            )
            return request

        now = utcnow()
        bound_hash = checkout_hash(request.checkout_jws)

        # The buyer's own signature over this exact basket — AP2's human-present
        # cart confirmation.
        request.checkout_mandate_jws = self.user_signer.sign(
            closed_checkout_mandate(cart=request.cart, delegate_chain=[bound_hash]),
            ttl_seconds=ONE_TIME_MANDATE_TTL_SECONDS,
            now=now,
        )

        # The narrowest authorisation that can fund this one purchase. See the
        # module docstring: approval is not an unlock.
        one_time_open = open_payment_mandate(
            budget=request.amount,
            amount_min=request.amount,
            amount_max=request.amount,
            allowed_payees=[request.cart.merchant_id],
            not_before=now - timedelta(seconds=60),
            not_after=now + timedelta(seconds=ONE_TIME_MANDATE_TTL_SECONDS),
            pinned_checkout_hash=bound_hash,
            cnf=self.user_signer.cnf,
        )
        one_time_open_jws = self.user_signer.sign(
            one_time_open, ttl_seconds=ONE_TIME_MANDATE_TTL_SECONDS, now=now
        )

        closed = closed_payment_mandate(
            payee=request.cart.merchant_id,
            payee_name=request.cart.merchant_name,
            amount=request.amount,
            payment_instrument="upi",
            checkout_hash=bound_hash,
            open_mandate_jws=one_time_open_jws,
            execution_date=now,
        )
        request.payment_mandate_jws = self.user_signer.sign(
            closed, ttl_seconds=ONE_TIME_MANDATE_TTL_SECONDS, now=now
        )
        request.payment_mandate_id = closed.mandate_id
        request.status = "approved"

        self.audit.append(
            ROLE_TRUSTED_SURFACE,
            Event.GATE_APPROVED,
            {
                "hold_id": hold_id,
                "checkout_id": request.checkout_id,
                "amount": request.amount,
                "merchant": request.cart.merchant_name,
                "payment_mandate_id": closed.mandate_id,
                "one_time_mandate_id": one_time_open.mandate_id,
                "scope": {
                    "amount_range": [request.amount, request.amount],
                    "budget": request.amount,
                    "allowed_payees": [request.cart.merchant_id],
                    "pinned_checkout": bound_hash[:16],
                    "expires_in_seconds": ONE_TIME_MANDATE_TTL_SECONDS,
                },
            },
            f"The buyer approved ₹{paise_to_inr_str(request.amount)} at "
            f"{request.cart.merchant_name}. Issued a single-use mandate for exactly that amount, "
            f"at that merchant, for that basket, valid {ONE_TIME_MANDATE_TTL_SECONDS // 60} "
            "minutes. The standing limit is unchanged.",
        )
        return request

    # -- rendering ----------------------------------------------------------

    def render(self, request: HeldRequest) -> str:
        """The approval page. A fixed template; every value is escaped.

        No model wrote any of this. The amount is the merchant's signed total, and
        the explanation is the verifier's own deterministic sentence.
        """
        lines = "".join(
            f"<tr><td>{html.escape(item.name)}<span class='sku'>{html.escape(item.sku)}</span></td>"
            f"<td class='n'>{item.qty}</td>"
            f"<td class='n'>₹{paise_to_inr_str(item.line_total)}</td></tr>"
            for item in request.cart.items
        )
        amount = paise_to_inr_str(request.amount)
        merchant = html.escape(request.cart.merchant_name)
        reason = html.escape(request.human_reason)
        constraint = html.escape(request.constraint_code)

        if request.status == "approved":
            banner = "<div class='done ok'>Approved. A single-use mandate was issued.</div>"
            actions = ""
        elif request.status == "denied":
            banner = "<div class='done no'>Declined. Nothing was charged.</div>"
            actions = ""
        elif request.status == "expired":
            banner = "<div class='done no'>This request expired before it was answered.</div>"
            actions = ""
        else:
            banner = ""
            actions = f"""
    <form method="post" action="/trusted-surface/{html.escape(request.hold_id)}/decision">
      <button class="approve" name="decision" value="approve" type="submit">
        Approve ₹{amount} once
      </button>
      <button class="deny" name="decision" value="deny" type="submit">Decline</button>
    </form>
    <p class="fine">
      Approving authorises exactly ₹{amount}, only at {merchant}, only for this basket,
      and only for the next {ONE_TIME_MANDATE_TTL_SECONDS // 60} minutes.
      Your standing limit does not change.
    </p>"""

        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approve a payment</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        max-width: 34rem; margin: 3rem auto; padding: 0 1.25rem; }}
 h1 {{ font-size: 1.3rem; margin: 0 0 .25rem; }}
 .lede {{ color: #666; margin: 0 0 1.5rem; }}
 .amount {{ font-size: 2.2rem; font-weight: 600; letter-spacing: -.02em; }}
 .card {{ border: 1px solid #d8d8d8; border-radius: 10px; padding: 1.1rem 1.25rem; margin: 1rem 0; }}
 table {{ width: 100%; border-collapse: collapse; margin-top: .5rem; }}
 td {{ padding: .35rem 0; border-bottom: 1px solid #eee; }}
 td.n {{ text-align: right; white-space: nowrap; }}
 .sku {{ display: block; font-size: .75rem; color: #888; font-family: ui-monospace, monospace; }}
 .why {{ background: #fff8e1; border-left: 3px solid #e0a800; padding: .75rem 1rem;
         border-radius: 0 6px 6px 0; }}
 .why code {{ font-size: .75rem; color: #666; }}
 button {{ font: inherit; padding: .7rem 1.2rem; border-radius: 8px; cursor: pointer;
           border: 1px solid transparent; margin-right: .5rem; }}
 .approve {{ background: #0b7a3d; color: #fff; }}
 .deny {{ background: #fff; color: #333; border-color: #ccc; }}
 .fine {{ font-size: .82rem; color: #666; }}
 .done {{ padding: .8rem 1rem; border-radius: 8px; font-weight: 600; }}
 .done.ok {{ background: #e6f4ea; color: #0b7a3d; }}
 .done.no {{ background: #fdecea; color: #a3231b; }}
 footer {{ margin-top: 2rem; font-size: .78rem; color: #999; }}
 @media (prefers-color-scheme: dark) {{
   body {{ background: #16181c; color: #e6e6e6; }}
   .card {{ border-color: #333; }} td {{ border-color: #2a2a2a; }}
   .why {{ background: #2a2410; }} .deny {{ background: #22252a; color: #ddd; border-color: #3a3a3a; }}
   .done.ok {{ background: #10301c; }} .done.no {{ background: #351613; color: #ff9d94; }}
 }}
</style></head>
<body>
  <h1>Approve a payment</h1>
  <p class="lede">Your shopping agent is asking for permission it does not already have.</p>
  {banner}
  <div class="card">
    <div class="amount">₹{amount}</div>
    <div>to <strong>{merchant}</strong></div>
    <table>{lines}</table>
  </div>
  <div class="why">
    {reason}
    <br><code>{constraint}</code>
  </div>
  {actions}
  <footer>
    AP2 Trusted Surface · checkout {html.escape(request.checkout_id)}<br>
    This page contains no AI. The amount above is taken from a merchant-signed
    Checkout Mandate and the explanation from the deterministic verifier.
  </footer>
</body></html>"""


def build_router(surface: TrustedSurface) -> APIRouter:
    """FastAPI routes for the approval page.

    Two HTML routes for the human and one JSON route the agent polls. The agent
    can *read* the outcome; it can never write one.
    """
    router = APIRouter(prefix="/trusted-surface", tags=["trusted-surface"])

    @router.get("/{hold_id}", response_class=HTMLResponse)
    def show(hold_id: str) -> HTMLResponse:
        try:
            request = surface.get(hold_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such approval request") from None
        return HTMLResponse(surface.render(request))

    @router.post("/{hold_id}/decision", response_class=HTMLResponse)
    def decide(hold_id: str, decision: str = Form(...)) -> HTMLResponse:
        if decision not in {"approve", "deny"}:
            raise HTTPException(status_code=400, detail="decision must be 'approve' or 'deny'")
        try:
            request = surface.decide(hold_id, approve=decision == "approve")
        except KeyError:
            raise HTTPException(status_code=404, detail="no such approval request") from None
        return HTMLResponse(surface.render(request))

    @router.get("/{hold_id}/status")
    def status(hold_id: str) -> JSONResponse:
        """What the shopping agent polls. Read-only, and it never returns a key."""
        try:
            request = surface.get(hold_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such approval request") from None
        return JSONResponse(request.as_dict())

    return router
