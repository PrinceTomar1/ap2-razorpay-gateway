"""The AP2 Merchant role: the seven operations, and the order they must happen in.

This is the composition point. Every other module in the repository does one
thing well and knows nothing about the others; this one wires them into the
lifecycle a purchase actually follows:

    search → check → assemble cart → create checkout (merchant signs the price)
      → complete checkout (buyer's standing authorisation is checked)
        → initiate payment (deterministic verifier → gate or rail → receipt)

Two rules hold throughout.

**Nothing reaches the payment rail before the verifier says ALLOW.** A malformed
mandate is rejected at :func:`gateway.mandates.verify_jws`; a well-formed one that
breaks a bound is rejected by :func:`gateway.verify.verify_payment_mandate`; only
an ``ALLOW`` reaches :class:`gateway.recovery.RecoveryPlaybook`.

**Every transition writes exactly one audit row, with a reason a person can
read.** The reason comes from :class:`llm.reason.ReasonWriter`, which falls back
to a deterministic template whenever a model is unavailable — so the audit trail
is complete whether or not anything is reachable.

The reason writer is the only thing here that can touch a model, and it is only
ever asked to phrase a sentence. It is never consulted about a number.
"""

from __future__ import annotations

from typing import Any

from ap2_min.builders import closed_checkout_mandate, open_checkout_mandate
from ap2_min.models import (
    Cart,
    CheckoutMandateContents,
    CheckoutReceiptContents,
    paise_to_inr_str,
)
from ap2_min.roles import ROLE_MERCHANT, ROLE_USER
from ap2_min.vct import VCT_CHECKOUT_CLOSED
from gateway.audit import AuditLog, Event
from gateway.ledger import Ledger
from gateway.mandates import (
    KeyRing,
    MandateError,
    Signer,
    checkout_hash,
    load_checkout_mandate,
    load_payment_mandate,
    new_id,
    utcnow,
)
from gateway.policy import Policy
from gateway.recovery import RecoveryPlaybook
from gateway.trusted_surface import TrustedSurface
from gateway.verify import Outcome, verify_payment_mandate
from llm.reason import ReasonWriter, template_only
from merchant.checkout import Catalog, CatalogError, CheckoutStore, ProductNotFound


class MerchantService:
    """One merchant-side façade over the catalogue, the verifier and the processor."""

    def __init__(
        self,
        *,
        catalog: Catalog,
        store: CheckoutStore,
        keyring: KeyRing,
        merchant_signer: Signer,
        playbook: RecoveryPlaybook,
        ledger: Ledger,
        audit: AuditLog,
        policy: Policy,
        trusted_surface: TrustedSurface,
        reason_writer: ReasonWriter | None = None,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.keyring = keyring
        self.signer = merchant_signer
        self.playbook = playbook
        self.processor = playbook.processor
        self.ledger = ledger
        self.audit = audit
        self.policy = policy
        self.trusted_surface = trusted_surface
        self.reasons = reason_writer or template_only()

    # -- 1. search ----------------------------------------------------------

    def search_inventory(
        self, query: str = "", filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Find products. Read-only, unauthenticated, and it signs nothing.

        ``filters`` accepts ``max_price_inr``/``min_price_inr`` in rupees (what an
        agent naturally has) and converts to paise here, so no caller outside this
        boundary handles rupees.
        """
        from ap2_min.models import inr

        options = dict(filters or {})
        max_price = options.pop("max_price_inr", None)
        min_price = options.pop("min_price_inr", None)
        results = self.catalog.search(
            query,
            max_price=inr(max_price) if max_price is not None else options.pop("max_price", None),
            min_price=inr(min_price) if min_price is not None else options.pop("min_price", None),
            category=options.pop("category", None),
            merchant_id=options.pop("merchant_id", None),
            size=options.pop("size", None),
            pincode=options.pop("pincode", None),
            in_stock_only=bool(options.pop("in_stock_only", True)),
            limit=int(options.pop("limit", 20)),
        )
        return {
            "query": query,
            "count": len(results),
            "results": [p.as_dict(stock=self.catalog.stock(p.sku)) for p in results],
        }

    # -- 2. check_product ---------------------------------------------------

    def check_product(self, product_id: str) -> dict[str, Any]:
        """One product, or a flat ``not_found``.

        Failure mode 7. An agent that hallucinates a SKU gets a cheap, typed no
        here — before a cart exists, before anything is signed, before the
        verifier is ever invoked. The correct cost of a made-up product id is one
        dictionary lookup.
        """
        try:
            product = self.catalog.get(product_id)
        except ProductNotFound as exc:
            payload = {"sku": product_id}
            self.audit.append(
                ROLE_MERCHANT,
                Event.PRODUCT_NOT_FOUND,
                payload,
                self.reasons.reason(Event.PRODUCT_NOT_FOUND, payload),
            )
            return exc.as_dict()
        return {"found": True, "product": product.as_dict(stock=self.catalog.stock(product_id))}

    # -- 3. check_serviceability -------------------------------------------

    def check_serviceability(self, pincode: str) -> dict[str, Any]:
        merchants = self.catalog.serviceable_merchants(pincode)
        return {
            "pincode": pincode,
            "serviceable": bool(merchants),
            "merchants": [
                {"id": m.id, "name": m.name, "city": m.city, "return_days": m.return_days}
                for m in merchants
            ],
        }

    # -- 4. assemble_cart ---------------------------------------------------

    def assemble_cart(
        self, items: list[dict[str, Any]], ship_to_pincode: str | None = None
    ) -> dict[str, Any]:
        """Price a cart and stamp the prices in. Single merchant only."""
        pincode = ship_to_pincode or self.policy.standing_authorisation.ship_to_pincode
        try:
            cart = self.store.assemble_cart(items, ship_to_pincode=pincode)
        except CatalogError as exc:
            return exc.as_dict()

        payload: dict[str, Any] = {
            "cart_id": cart.cart_id,
            "merchant": cart.merchant_name,
            "item_count": len(cart.items),
            "total": cart.total,
            "ship_to_pincode": cart.ship_to_pincode,
        }
        self.audit.append(
            ROLE_MERCHANT,
            Event.CART_ASSEMBLED,
            payload,
            self.reasons.reason(Event.CART_ASSEMBLED, payload),
        )
        return {"cart": _cart_dict(cart)}

    # -- 5. create_checkout -------------------------------------------------

    def create_checkout(self, cart_id: str) -> dict[str, Any]:
        """Sign the cart. This is the merchant's price and availability guarantee.

        Returns two things, per the AP2 checkout flow:

        1. ``checkout_mandate_jwt`` — a merchant-signed **closed** Checkout Mandate
           (``mandate.checkout.1``) over this exact cart, valid for
           ``mandates.checkout_ttl_seconds``. Its hash is what a Payment Mandate
           binds to, so this signature is what makes cart substitution detectable.
        2. ``open_checkout_mandate_template`` — the **open** Checkout Mandate the
           buyer's standing authorisation must satisfy for this cart to go through:
           which merchant, what ceiling, which pincode. Unsigned, because only the
           buyer can sign an open mandate. Handing it back lets the agent see
           immediately whether it needs to escalate, rather than discovering it by
           being refused.
        """
        try:
            cart = self.store.cart(cart_id)
        except CatalogError as exc:
            return exc.as_dict()

        available, reason = self.store.recheck(cart)
        if not available:
            payload = {"cart_id": cart_id, "reason": reason}
            self.audit.append(
                ROLE_MERCHANT,
                Event.STOCK_RECHECK_FAILED,
                payload,
                self.reasons.reason(Event.STOCK_RECHECK_FAILED, payload),
            )
            return {"error": "stock.unavailable", "message": reason, "cart_id": cart_id}

        record = self.store.open_checkout(cart)
        ttl = self.policy.mandates.checkout_ttl_seconds
        contents = closed_checkout_mandate(cart=cart)
        record.checkout_jws = self.signer.sign(contents, ttl_seconds=ttl)

        template = open_checkout_mandate(
            allowed_merchants=[cart.merchant_id],
            max_amount=cart.total,
            ship_to_pincode=cart.ship_to_pincode,
        )

        created: dict[str, Any] = {
            "checkout_id": record.checkout_id,
            "cart_id": cart.cart_id,
            "merchant": cart.merchant_name,
            "total": cart.total,
            "ttl_minutes": ttl // 60,
            "checkout_hash": checkout_hash(record.checkout_jws)[:16],
        }
        self.audit.append(
            ROLE_MERCHANT,
            Event.CHECKOUT_CREATED,
            created,
            self.reasons.reason(Event.CHECKOUT_CREATED, created),
        )
        return {
            "checkout_id": record.checkout_id,
            "checkout_mandate_jwt": record.checkout_jws,
            "checkout_hash": checkout_hash(record.checkout_jws),
            "expires_in_seconds": ttl,
            "cart": _cart_dict(cart),
            "open_checkout_mandate_template": template.model_dump(mode="json"),
        }

    # -- 6. complete_checkout ----------------------------------------------

    def complete_checkout(self, checkout_id: str, checkout_mandate_jwt: str) -> dict[str, Any]:
        """Check the buyer's Checkout Mandate against this cart.

        Accepts either the buyer's **open** standing authorisation (the normal
        path) or a buyer-signed **closed** Checkout Mandate over this exact cart
        (the path after a Trusted Surface approval).

        A cart that falls outside the standing authorisation returns AP2's
        ``unresolved_constraint`` — naming the constraint that is unresolved — and
        not a denial. The buyer has not refused anything; they simply have not
        been asked yet.
        """
        try:
            record = self.store.checkout(checkout_id)
        except CatalogError as exc:
            return exc.as_dict()
        cart = record.cart

        try:
            mandate, _claims = load_checkout_mandate(
                checkout_mandate_jwt, self.keyring, expected_role=ROLE_USER
            )
        except MandateError as exc:
            payload = {"checkout_id": checkout_id, "reason": exc.message, "code": exc.code}
            self.audit.append(
                ROLE_MERCHANT,
                Event.MANDATE_REJECTED,
                payload,
                self.reasons.reason(Event.MANDATE_REJECTED, payload),
            )
            return exc.as_dict()

        received: dict[str, Any] = {
            "checkout_id": checkout_id,
            "vct": mandate.vct,
            "mandate_id": mandate.mandate_id,
            "total": cart.total,
        }
        self.audit.append(
            ROLE_MERCHANT,
            Event.CHECKOUT_MANDATE_RECEIVED,
            received,
            self.reasons.reason(Event.CHECKOUT_MANDATE_RECEIVED, received),
        )

        unresolved = self._checkout_gap(mandate, cart, record.checkout_jws or "")
        if unresolved is not None:
            code, reason = unresolved
            # Raise the gate as early as it can honestly be raised. The basket is
            # already outside the buyer's standing authorisation, so there is no
            # point building a Payment Mandate that is certain to be refused —
            # ask the human now, while the merchant's price guarantee is fresh.
            hold = self.trusted_surface.hold(
                checkout_id=checkout_id,
                checkout_jws=record.checkout_jws or "",
                cart=cart,
                constraint_code=code,
                human_reason=reason,
            )
            unresolved_payload: dict[str, Any] = {
                "checkout_id": checkout_id,
                "constraint": code,
                "reason": reason,
                "hold_id": hold.hold_id,
            }
            self.audit.append(
                ROLE_MERCHANT,
                Event.CHECKOUT_UNRESOLVED,
                unresolved_payload,
                self.reasons.reason(Event.CHECKOUT_UNRESOLVED, unresolved_payload),
            )
            return {
                "error": "unresolved_constraint",
                "constraint": code,
                "human_reason": reason,
                "checkout_id": checkout_id,
                "amount": cart.total,
                "currency": cart.currency,
                "hold_id": hold.hold_id,
                "approval_url": self.trusted_surface.url_for(hold.hold_id),
            }

        available, reason = self.store.recheck(cart)
        if not available:
            payload = {"checkout_id": checkout_id, "reason": reason}
            self.audit.append(
                ROLE_MERCHANT,
                Event.STOCK_RECHECK_FAILED,
                payload,
                self.reasons.reason(Event.STOCK_RECHECK_FAILED, payload),
            )
            return {"error": "stock.unavailable", "message": reason, "checkout_id": checkout_id}

        record.status = "confirmed"
        record.open_checkout_mandate_hash = checkout_hash(checkout_mandate_jwt)
        receipt = CheckoutReceiptContents(
            receipt_id=new_id("crcpt"),
            checkout_id=checkout_id,
            merchant_id=cart.merchant_id,
            merchant_name=cart.merchant_name,
            cart_id=cart.cart_id,
            amount=cart.total,
            checkout_hash=checkout_hash(record.checkout_jws or ""),
            open_checkout_mandate_hash=record.open_checkout_mandate_hash,
            ts=utcnow(),
        )
        receipt_jws = self.signer.sign(
            receipt, ttl_seconds=self.policy.mandates.checkout_ttl_seconds
        )
        issued: dict[str, Any] = {
            "checkout_id": checkout_id,
            "receipt_id": receipt.receipt_id,
            "merchant": cart.merchant_name,
            "total": cart.total,
        }
        self.audit.append(
            ROLE_MERCHANT,
            Event.CHECKOUT_RECEIPT_ISSUED,
            issued,
            self.reasons.reason(Event.CHECKOUT_RECEIPT_ISSUED, issued),
        )
        return {
            "checkout_receipt": receipt.model_dump(mode="json"),
            "checkout_receipt_jws": receipt_jws,
            "status": "confirmed",
        }

    def _checkout_gap(
        self, mandate: CheckoutMandateContents, cart: Cart, merchant_checkout_jws: str
    ) -> tuple[str, str] | None:
        """Return ``(code, reason)`` if this cart falls outside the mandate.

        Split out because it is pure: given a mandate and a cart it always gives
        the same answer, which makes the gate testable without a service.
        """
        if mandate.vct == VCT_CHECKOUT_CLOSED:
            # A buyer-signed closed mandate must be about *this* basket.
            if mandate.cart is None or mandate.cart.cart_id != cart.cart_id:
                return (
                    "checkout.wrong_cart",
                    "The buyer's confirmation is for a different basket.",
                )
            if mandate.cart.total != cart.total:
                return (
                    "checkout.price_changed",
                    f"The buyer confirmed ₹{paise_to_inr_str(mandate.cart.total)} but this "
                    f"basket is now ₹{paise_to_inr_str(cart.total)}.",
                )
            expected = checkout_hash(merchant_checkout_jws)
            if expected not in mandate.delegate_chain:
                return (
                    "checkout.unlinked_confirmation",
                    "The buyer's confirmation is not linked to this signed checkout.",
                )
            return None

        # Nothing below needs a vct guard: CheckoutVct is a two-value Literal and
        # the closed case returned above, so this is the open case by construction.
        # (An earlier version had an unreachable `!= VCT_CHECKOUT_OPEN` branch here,
        # carrying a `# pragma: no cover` that hid the fact it was dead. Strict
        # mypy found it.)
        if cart.merchant_id not in (mandate.allowed_merchants or []):
            return (
                "checkout.merchant_outside_standing_scope",
                f"{cart.merchant_name} is not one of the shops this standing authorisation "
                "covers, so it needs your approval.",
            )
        ceiling = mandate.max_amount or 0
        if cart.total > ceiling:
            return (
                "checkout.amount_exceeds_standing_limit",
                f"₹{paise_to_inr_str(cart.total)} is above the "
                f"₹{paise_to_inr_str(ceiling)} per-checkout limit on this standing "
                "authorisation, so it needs your approval.",
            )
        if mandate.ship_to_pincode and mandate.ship_to_pincode != cart.ship_to_pincode:
            return (
                "checkout.pincode_outside_standing_scope",
                f"This standing authorisation ships to {mandate.ship_to_pincode}, not "
                f"{cart.ship_to_pincode}, so it needs your approval.",
            )
        return None

    # -- 7. initiate_payment ------------------------------------------------

    def initiate_payment(self, checkout_id: str, payment_mandate_jwt: str) -> dict[str, Any]:
        """Verify, then pay. The only entry point to the money path.

        The order of the first three steps is load-bearing:

        1. **Envelope.** A malformed or forged mandate is refused here, with a
           typed code, before anything else happens. Failure mode 3.
        2. **Idempotency.** A mandate that has already been settled returns its
           original receipt — *before* the verifier runs, so a duplicate submit
           does not trip replay detection on its own nonce. Failure mode 6.
        3. **Stock.** Re-read live, before the verifier, so a sold-out cart is a
           clean decline that never burns a mandate. Failure mode 5. (The recovery
           playbook re-checks again before each retry; the two cover different
           windows.)
        """
        try:
            record = self.store.checkout(checkout_id)
        except CatalogError as exc:
            return exc.as_dict()
        cart = record.cart

        if record.status not in {"confirmed", "paid"}:
            return {
                "error": "checkout.not_confirmed",
                "message": (
                    "This checkout has not been confirmed against the buyer's Checkout "
                    "Mandate yet. Call complete_checkout first."
                ),
                "status": record.status,
            }

        # --- 1. Envelope ---------------------------------------------------
        try:
            presented, _ = load_payment_mandate(payment_mandate_jwt, self.keyring)
        except MandateError as exc:
            payload = {"checkout_id": checkout_id, "code": exc.code, "reason": exc.message}
            self.audit.append(
                ROLE_MERCHANT,
                Event.MANDATE_REJECTED,
                payload,
                self.reasons.reason(Event.MANDATE_REJECTED, payload),
            )
            return exc.as_dict()

        received: dict[str, Any] = {
            "checkout_id": checkout_id,
            "payment_mandate_id": presented.mandate_id,
            "vct": presented.vct,
            "amount": presented.payment_amount or cart.total,
            "payee": presented.payee or cart.merchant_id,
        }
        self.audit.append(
            ROLE_MERCHANT,
            Event.PAYMENT_MANDATE_RECEIVED,
            received,
            self.reasons.reason(Event.PAYMENT_MANDATE_RECEIVED, received),
        )

        # --- 2. Idempotency, before the verifier ---------------------------
        settled = self.processor.existing_outcome(presented.mandate_id)
        if settled is not None:
            return {
                "payment_receipt": settled.receipt.model_dump(mode="json"),
                "payment_receipt_jws": settled.receipt_jws,
                "status": settled.receipt.status,
                "replayed": True,
                "human_reason": (
                    "This payment mandate was already settled; returning the original receipt."
                ),
            }

        # --- 3. Stock, before the verifier ---------------------------------
        available, stock_reason = self.store.recheck(cart)
        if not available:
            stock_payload: dict[str, Any] = {
                "checkout_id": checkout_id,
                "payment_mandate_id": presented.mandate_id,
                "reason": stock_reason,
                "charged": False,
            }
            self.audit.append(
                ROLE_MERCHANT,
                Event.STOCK_RECHECK_FAILED,
                stock_payload,
                self.reasons.reason(Event.STOCK_RECHECK_FAILED, stock_payload),
            )
            # The checkout status is deliberately NOT changed. The merchant's
            # signed price guarantee still stands for its full window, and the
            # stock re-check runs again on every subsequent attempt — so if the
            # shelf is restocked in time the sale can still complete, and if it is
            # not, this same branch refuses again. Marking it dead here would lose
            # a legitimate sale for no safety benefit.
            return {
                "error": "stock.unavailable",
                "message": stock_reason,
                "checkout_id": checkout_id,
                "charged": False,
            }

        # --- 4. The deterministic verifier ---------------------------------
        decision = verify_payment_mandate(
            payment_mandate_jwt,
            record.checkout_jws or "",
            self.ledger,
            keyring=self.keyring,
            clock_skew_seconds=self.policy.mandates.clock_skew_seconds,
            checkout_total=cart.total,
        )
        for check in decision.checks:
            self.audit.append(
                "verifier",
                Event.CHECK_RESULT,
                {"checkout_id": checkout_id, **check.as_dict()},
                check.human_reason
                or f"Check '{check.name}' {'passed' if check.passed else 'failed'}.",
            )
        decision_payload: dict[str, Any] = {
            "checkout_id": checkout_id,
            "outcome": decision.outcome.value,
            "code": decision.code,
            "reason": decision.human_reason or "",
            "amount": decision.amount or cart.total,
        }
        self.audit.append(
            "verifier",
            Event.DECISION,
            decision_payload,
            decision.human_reason or self.reasons.reason(Event.DECISION, decision_payload),
        )

        # --- 5a. The gate ---------------------------------------------------
        if decision.outcome is Outcome.UNRESOLVED_CONSTRAINT:
            hold = self.trusted_surface.hold(
                checkout_id=checkout_id,
                checkout_jws=record.checkout_jws or "",
                cart=cart,
                constraint_code=decision.code,
                human_reason=decision.human_reason or "This purchase needs your approval.",
            )
            body = decision.error_response()
            body |= {
                "checkout_id": checkout_id,
                "hold_id": hold.hold_id,
                "approval_url": self.trusted_surface.url_for(hold.hold_id),
            }
            return body

        # --- 5b. Refusal ----------------------------------------------------
        if decision.outcome is Outcome.DENY:
            # The checkout survives a refused mandate. `status` describes the
            # checkout, not the outcome of one presentation: an agent that
            # presents a malformed or over-limit mandate can present a correct one
            # next, and letting a single bad presentation invalidate the checkout
            # would hand anyone who can call this endpoint a way to kill a
            # stranger's cart.
            return {**decision.error_response(), "checkout_id": checkout_id, "charged": False}

        # --- 6. Pay, with bounded recovery ---------------------------------
        checkout_contents = closed_checkout_mandate(cart=cart)
        result = self.playbook.run(
            decision, checkout_contents, stock_check=lambda: self.store.recheck(cart)
        )

        if result.deferred:
            return {
                "status": "deferred",
                "checkout_id": checkout_id,
                "human_reason": result.human_reason,
                "retry_after_seconds": 30,
                "mandate_spent": False,
            }

        assert result.outcome is not None
        if result.captured:
            remaining = self.store.commit_stock(checkout_id)
            decremented: dict[str, Any] = {
                "checkout_id": checkout_id,
                "remaining_stock": remaining,
                "detail": ", ".join(f"{sku}={qty}" for sku, qty in sorted(remaining.items())),
            }
            self.audit.append(
                ROLE_MERCHANT,
                Event.STOCK_DECREMENTED,
                decremented,
                self.reasons.reason(Event.STOCK_DECREMENTED, decremented),
            )

        return {
            "payment_receipt": result.outcome.receipt.model_dump(mode="json"),
            "payment_receipt_jws": result.outcome.receipt_jws,
            "status": result.outcome.receipt.status,
            "attempts": result.attempts,
            "methods_tried": list(result.methods_tried),
            "recovered": result.recovered,
            "replayed": result.outcome.replayed,
            "human_reason": result.human_reason,
            "checkout_id": checkout_id,
        }


def _cart_dict(cart: Cart) -> dict[str, Any]:
    body = cart.model_dump(mode="json")
    body["total_inr"] = paise_to_inr_str(cart.total)
    return body
