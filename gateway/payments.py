"""The Merchant Payment Processor: the only code in this repository that spends money.

Two invariants, and everything here exists to hold them:

**1. Funds move only after ALLOW.** :meth:`PaymentProcessor.execute_payment`
raises :class:`PaymentNotAuthorized` if handed anything but an ``ALLOW``
decision. Not a log line, not a warning — an exception, before the rail is
touched. A caller that gets the flow wrong fails loudly rather than quietly
charging someone.

**2. One Payment Mandate can charge at most once, ever.** The idempotency key is
``sha256(payment_mandate.id)``, and it guards three distinct dangers:

* *Duplicate submit.* The same mandate presented twice returns the first receipt,
  including a failed one. Failure mode 6.
* *Retry after decline.* Recovery may try another instrument under the same key,
  but only after proving no earlier attempt captured.
* *Retry after timeout.* The dangerous one. A timeout means we do not know
  whether money moved, so before every new order we ask the rail about every
  order already created under this key. If any captured, we stop and return that
  capture. This is the "capture probe", and it is why a retry storm cannot become
  a double charge.

There is no language model anywhere in this module, and no import path from here
reaches ``llm/``. Deciding whether to release funds, and how much, is arithmetic
over signed data — the one place where a probabilistic component would be
indefensible.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ap2_min.models import (
    CheckoutMandateContents,
    PaymentReceiptContents,
    paise_to_inr_str,
)
from ap2_min.roles import ROLE_MPP
from gateway.audit import AuditLog, Event
from gateway.ledger import IdempotencyRecord, Ledger
from gateway.mandates import Signer, new_id, utcnow
from gateway.razorpay_client import (
    METHOD_UPI,
    TEST_VPA_SUCCESS,
    Payment,
    PaymentRail,
    RailDeclined,
    RailError,
)
from gateway.verify import Decision

#: Receipts are records, not credentials. They outlive the mandates they attest
#: to, so a dispute six months from now can still be settled from the signature.
RECEIPT_TTL_SECONDS = 365 * 24 * 3600


#: How long one attempt may hold the lease on an idempotency key before a
#: successor is allowed to take over. Longer than any single rail call; short
#: enough that a crashed process does not wedge a mandate for a whole day.
ATTEMPT_LEASE_SECONDS = 120.0

#: How long a concurrent presentation waits for the in-flight one to finish
#: before giving up. It waits rather than erroring because the overwhelmingly
#: likely outcome is "the other request is about to produce the receipt I want".
CONCURRENT_WAIT_SECONDS = 30.0


class PaymentNotAuthorized(RuntimeError):
    """execute_payment was called without an ALLOW decision. A programming error."""


class ConcurrentAttemptError(RuntimeError):
    """Another attempt on this mandate held the lease for longer than we would wait.

    Deliberately not a retry: we do not know whether that attempt charged, and
    guessing is the one thing this module must never do.
    """


def idempotency_key(payment_mandate_id: str) -> str:
    """``sha256(payment_mandate.id)`` — the root that binds every attempt together.

    Derived from the mandate id rather than generated per request, so that a
    client retrying blind, a recovery attempt, and a duplicate submit all land on
    the same key without having to coordinate.
    """
    return hashlib.sha256(payment_mandate_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PaymentOutcome:
    """A terminal result plus how we got there."""

    receipt: PaymentReceiptContents
    receipt_jws: str
    idempotency_key: str
    replayed: bool = False
    attempts: int = 1

    @property
    def captured(self) -> bool:
        return self.receipt.status == "captured"

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt": self.receipt.model_dump(mode="json"),
            "receipt_jws": self.receipt_jws,
            "idempotency_key": self.idempotency_key,
            "replayed": self.replayed,
            "attempts": self.attempts,
        }


class PaymentProcessor:
    """Turns an ALLOW into at most one captured payment, and a receipt either way."""

    def __init__(
        self,
        *,
        rail: PaymentRail,
        ledger: Ledger,
        audit: AuditLog,
        signer: Signer,
        lease_seconds: float = ATTEMPT_LEASE_SECONDS,
        concurrent_wait_seconds: float = CONCURRENT_WAIT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rail = rail
        self.ledger = ledger
        self.audit = audit
        self.signer = signer
        self.lease_seconds = lease_seconds
        self.concurrent_wait_seconds = concurrent_wait_seconds
        self._sleep = sleep
        self._clock = clock

    # -- idempotency --------------------------------------------------------

    def existing_outcome(self, payment_mandate_id: str) -> PaymentOutcome | None:
        """The terminal receipt already issued for this mandate, if any.

        Callers check this *before* running the verifier, so a duplicate submit
        returns the original answer rather than tripping replay detection on its
        own nonce. Idempotency and replay detection answer different questions;
        see :func:`gateway.verify.check_nonce_unused`.
        """
        key = idempotency_key(payment_mandate_id)
        record = self.ledger.get_idempotency(key)
        if record is None or not record.is_terminal or record.receipt is None:
            return None
        return PaymentOutcome(
            receipt=PaymentReceiptContents.model_validate(record.receipt),
            receipt_jws=record.receipt_jws or "",
            idempotency_key=key,
            replayed=True,
            attempts=record.attempts,
        )

    # -- the money path -----------------------------------------------------

    def execute_payment(
        self,
        decision: Decision,
        checkout: CheckoutMandateContents,
        *,
        method: str | None = None,
        vpa: str | None = None,
        human_reason: str | None = None,
    ) -> PaymentOutcome:
        """Charge for an ALLOWed decision. At most once, ever.

        Raises :class:`~gateway.razorpay_client.RailDeclined` on a decline and
        :class:`~gateway.razorpay_client.RailTimeout` on an unknown outcome —
        those are for :mod:`gateway.recovery` to interpret, because *whether to
        try again* is a policy question and this function only knows how to
        charge once.
        """
        if not decision.allowed:
            raise PaymentNotAuthorized(
                f"execute_payment requires an ALLOW decision, got {decision.outcome.value} "
                f"({decision.code}). Funds must never move without one."
            )
        assert decision.payment_mandate_id is not None
        assert decision.amount is not None
        assert decision.payee is not None
        assert decision.bound_checkout_hash is not None

        key = idempotency_key(decision.payment_mandate_id)
        record = self._claim_exclusively(key, decision.payment_mandate_id)

        # --- 1. Already terminal? Return the original answer, charge nothing.
        if record.is_terminal and record.receipt is not None:
            self.audit.append(
                ROLE_MPP,
                Event.IDEMPOTENT_REPLAY,
                {
                    "idempotency_key": key,
                    "payment_mandate_id": decision.payment_mandate_id,
                    "status": record.status,
                    "returned_receipt_id": record.receipt.get("receipt_id"),
                },
                f"This payment mandate was already settled as '{record.status}'. Returning the "
                "original receipt without contacting the rail.",
            )
            return PaymentOutcome(
                receipt=PaymentReceiptContents.model_validate(record.receipt),
                receipt_jws=record.receipt_jws or "",
                idempotency_key=key,
                replayed=True,
                attempts=record.attempts,
            )

        try:
            return self._attempt(decision, checkout, key, record, method, vpa, human_reason)
        finally:
            self.ledger.release_attempt_lease(key)

    def _attempt(
        self,
        decision: Decision,
        checkout: CheckoutMandateContents,
        key: str,
        record: IdempotencyRecord,
        method: str | None,
        vpa: str | None,
        human_reason: str | None,
    ) -> PaymentOutcome:
        """One attempt, under the lease. Split out so the lease release is a finally."""
        # execute_payment established these; restated so this function reads on
        # its own and so the type checker can see it too.
        assert decision.payment_mandate_id is not None
        assert decision.amount is not None
        assert decision.payee is not None
        assert decision.bound_checkout_hash is not None

        # --- 2. Burn the nonce on the first attempt only.
        # Recovery re-enters this function for the same mandate; the nonce is per
        # *mandate*, not per attempt, so burning it again would deny our own retry.
        if record.attempts == 0:
            self.ledger.burn_nonce(decision.nonce or "", decision.payment_mandate_id)

        # --- 3. Capture probe. Did an earlier attempt under this key succeed?
        prior = self._find_prior_capture(record.order_ids)
        if prior is not None:
            self.audit.append(
                ROLE_MPP,
                Event.RECOVERY_ABORTED_PRIOR_CAPTURE,
                {
                    "idempotency_key": key,
                    "order_id": prior.order_id,
                    "payment_id": prior.id,
                    "probed_orders": record.order_ids,
                },
                f"An earlier attempt on {prior.order_id} had in fact captured — stopping here "
                "rather than charging a second time.",
            )
            return self._settle_captured(
                decision, checkout, prior, key, attempts=record.attempts, replayed=True
            )

        chosen_method = method or decision.payment_instrument or METHOD_UPI

        # --- 4. Create the order.
        order = self.rail.create_order(
            amount=decision.amount,
            currency=decision.currency,
            # The receipt carries the idempotency key, so an operator looking at
            # the Razorpay dashboard can trace any order back to the mandate that
            # authorised it without access to our database.
            receipt=key[:40],
            notes={
                "reference": decision.payment_mandate_id,
                "checkout_hash": decision.bound_checkout_hash[:40],
                "open_mandate": (decision.open_mandate_id or "")[:40],
                "protocol": "ap2-v0.2",
            },
        )
        self.ledger.note_order(key, order.id)
        self.audit.append(
            ROLE_MPP,
            Event.ORDER_CREATED,
            {
                "order_id": order.id,
                "amount": order.amount,
                "currency": order.currency,
                "method": chosen_method,
                "idempotency_key": key,
                "rail": self.rail.name,
                "cart_id": checkout.cart.cart_id if checkout.cart else None,
                "merchant": checkout.cart.merchant_name if checkout.cart else None,
            },
            human_reason
            or (
                f"Created a ₹{paise_to_inr_str(order.amount)} order on the {self.rail.name} rail "
                f"for {decision.payee}, to be paid by {chosen_method}."
            ),
        )

        # --- 5. Attempt it.
        self.audit.append(
            ROLE_MPP,
            Event.PAYMENT_ATTEMPT,
            {
                "order_id": order.id,
                "method": chosen_method,
                "attempt": record.attempts + 1,
                "idempotency_key": key,
            },
            f"Attempt {record.attempts + 1} on {order.id} using {chosen_method}.",
        )
        try:
            payment = self.rail.complete_test_payment(
                order.id,
                method=chosen_method,
                vpa=vpa or (TEST_VPA_SUCCESS if chosen_method == METHOD_UPI else None),
            )
        except RailDeclined as exc:
            self.audit.append(
                ROLE_MPP,
                Event.PAYMENT_DECLINED,
                {
                    "order_id": order.id,
                    "method": chosen_method,
                    "error": exc.code,
                    "payment_id": exc.payment.id if exc.payment else None,
                    "idempotency_key": key,
                },
                f"{chosen_method} was declined on {order.id}: {exc.message}. No money moved.",
            )
            raise
        except RailError as exc:
            self.audit.append(
                ROLE_MPP,
                Event.RAIL_TIMEOUT,
                {
                    "order_id": order.id,
                    "method": chosen_method,
                    "error": exc.code,
                    "idempotency_key": key,
                },
                f"The rail did not give a definite answer for {order.id} ({exc.code}). The "
                "outcome is unknown, so this order will be probed before any retry.",
            )
            raise

        return self._settle_captured(decision, checkout, payment, key, attempts=record.attempts + 1)

    def _claim_exclusively(self, key: str, payment_mandate_id: str) -> IdempotencyRecord:
        """Claim the key and take the attempt lease, waiting for a concurrent holder.

        Returns as soon as either (a) the record is terminal — someone else
        finished, and we will return their receipt — or (b) we hold the lease.
        """
        deadline = self._clock() + self.concurrent_wait_seconds
        while True:
            record = self.ledger.claim(key, payment_mandate_id)
            if record.is_terminal:
                return record
            if self.ledger.acquire_attempt_lease(key, lease_seconds=self.lease_seconds):
                return record
            if self._clock() >= deadline:
                raise ConcurrentAttemptError(
                    f"another attempt on payment mandate {payment_mandate_id} has held the "
                    f"lease for over {self.concurrent_wait_seconds:.0f}s. Refusing to start a "
                    "second one: its outcome is unknown."
                )
            self._sleep(0.01)

    # -- terminal outcomes --------------------------------------------------

    def finalise_failure(
        self,
        decision: Decision,
        checkout: CheckoutMandateContents,
        *,
        failure_code: str,
        failure_reason: str,
        attempts: int,
        method: str | None = None,
    ) -> PaymentOutcome:
        """Issue a signed ``payment_failed`` receipt and close the key.

        A failure gets a receipt for the same reason a success does: an agent that
        asked for money and got silence cannot tell "declined" from "lost in
        transit", and that ambiguity is exactly how double charges happen. The
        receipt is a *contract* that nothing was charged.
        """
        assert decision.payment_mandate_id is not None
        key = idempotency_key(decision.payment_mandate_id)
        record = self.ledger.get_idempotency(key)
        if record is not None and record.is_terminal and record.receipt is not None:
            return PaymentOutcome(
                receipt=PaymentReceiptContents.model_validate(record.receipt),
                receipt_jws=record.receipt_jws or "",
                idempotency_key=key,
                replayed=True,
                attempts=record.attempts,
            )
        self.ledger.claim(key, decision.payment_mandate_id)

        receipt = PaymentReceiptContents(
            receipt_id=new_id("rcpt"),
            status="failed",
            payment_mandate_id=decision.payment_mandate_id,
            idempotency_key=key,
            amount=decision.amount or 0,
            currency=decision.currency,
            payee=decision.payee or "",
            method=method,
            checkout_hash=decision.bound_checkout_hash or "",
            attempts=max(attempts, 1),
            failure_code=failure_code,
            failure_reason=failure_reason,
            ts=utcnow(),
        )
        return self._issue(receipt, key, replayed=False)

    def _settle_captured(
        self,
        decision: Decision,
        checkout: CheckoutMandateContents,
        payment: Payment,
        key: str,
        *,
        attempts: int,
        replayed: bool = False,
    ) -> PaymentOutcome:
        # Budget is consumed here and nowhere else: only a real capture counts
        # towards payment.budget. A decline moves no money and must not eat into
        # the user's daily limit.
        assert decision.payment_mandate_id is not None
        self.ledger.record_spend(
            open_mandate_id=decision.open_mandate_id or "",
            payment_mandate_id=decision.payment_mandate_id,
            amount=payment.amount,
            currency=payment.currency,
            payee=decision.payee or "",
        )
        self.audit.append(
            ROLE_MPP,
            Event.PAYMENT_CAPTURED,
            {
                "order_id": payment.order_id,
                "payment_id": payment.id,
                "amount": payment.amount,
                "currency": payment.currency,
                "method": payment.method,
                "idempotency_key": key,
                "cart_id": checkout.cart.cart_id if checkout.cart else None,
            },
            f"Captured ₹{paise_to_inr_str(payment.amount)} for "
            f"{checkout.cart.merchant_name if checkout.cart else decision.payee} "
            f"({payment.id}) by {payment.method}.",
        )
        receipt = PaymentReceiptContents(
            receipt_id=new_id("rcpt"),
            status="captured",
            payment_mandate_id=decision.payment_mandate_id,
            idempotency_key=key,
            amount=payment.amount,
            currency=payment.currency,
            payee=decision.payee or "",
            order_id=payment.order_id,
            payment_id=payment.id,
            method=payment.method,
            checkout_hash=decision.bound_checkout_hash or "",
            attempts=max(attempts, 1),
            ts=utcnow(),
        )
        return self._issue(receipt, key, replayed=replayed)

    def _issue(
        self, receipt: PaymentReceiptContents, key: str, *, replayed: bool
    ) -> PaymentOutcome:
        """Sign the receipt, close the idempotency key, log it. In that order."""
        receipt_jws = self.signer.sign(receipt, ttl_seconds=RECEIPT_TTL_SECONDS)
        payload = receipt.model_dump(mode="json")
        self.ledger.finalise(key, status=receipt.status, receipt_jws=receipt_jws, receipt=payload)
        self.audit.append(
            ROLE_MPP,
            Event.PAYMENT_RECEIPT_ISSUED,
            {
                "receipt_id": receipt.receipt_id,
                "status": receipt.status,
                "amount": receipt.amount,
                "payment_id": receipt.payment_id,
                "order_id": receipt.order_id,
                "attempts": receipt.attempts,
                "failure_code": receipt.failure_code,
                "idempotency_key": key,
            },
            (
                f"Issued a signed receipt: ₹{paise_to_inr_str(receipt.amount)} "
                f"{receipt.status} after {receipt.attempts} attempt(s)."
                + (f" Reason: {receipt.failure_reason}" if receipt.failure_reason else "")
            ),
        )
        return PaymentOutcome(
            receipt=receipt,
            receipt_jws=receipt_jws,
            idempotency_key=key,
            replayed=replayed,
            attempts=receipt.attempts,
        )

    def _find_prior_capture(self, order_ids: list[str]) -> Payment | None:
        """Ask the rail whether any order under this key already captured.

        Runs before every new order. If the rail is unreachable we return None and
        let the attempt proceed — the rail being down is also the reason we could
        not have charged anything through it.
        """
        for order_id in order_ids:
            try:
                payments = self.rail.fetch_order_payments(order_id)
            except RailError:
                continue
            for payment in payments:
                if payment.is_captured:
                    return payment
        return None
