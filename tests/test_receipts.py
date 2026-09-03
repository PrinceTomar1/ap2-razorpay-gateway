"""Receipts: the externally verifiable half of the audit trail.

The audit chain proves *to us* that our own records are intact. A receipt proves
something stronger and to somebody else: anyone holding the merchant's or the
processor's public key can verify what was agreed and what was charged, without
access to our database, our logs, or our goodwill.
"""

from __future__ import annotations

from typing import Any

import pytest

from ap2_min.models import (
    CheckoutReceiptContents,
    PaymentReceiptContents,
    inr,
)
from ap2_min.roles import ROLE_MERCHANT, ROLE_MPP
from ap2_min.vct import VCT_CHECKOUT_RECEIPT, VCT_PAYMENT_RECEIPT
from gateway.bootstrap import Gateway
from gateway.mandates import (
    MandateSignatureError,
    UntrustedIssuerError,
    checkout_hash,
    verify_and_load,
)


def _buy(wired: Gateway, sku: str = "SF-RUN-001") -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one clean purchase and return (checkout response, payment response)."""
    from ap2_min.builders import closed_payment_mandate
    from gateway.mandates import utcnow

    merchant = wired.merchant
    cart = merchant.assemble_cart([{"sku": sku, "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    confirmed = merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)
    now = utcnow()
    mandate = closed_payment_mandate(
        payee=cart["merchant_id"],
        payee_name=cart["merchant_name"],
        amount=cart["total"],
        payment_instrument="upi",
        checkout_hash=checkout["checkout_hash"],
        open_mandate_jws=wired.open_payment_jws,
        execution_date=now,
    )
    paid = merchant.initiate_payment(
        checkout["checkout_id"], wired.agent.sign(mandate, ttl_seconds=600, now=now)
    )
    return {**checkout, **confirmed}, paid


# ---------------------------------------------------------------------------
# Checkout receipts
# ---------------------------------------------------------------------------


def test_a_checkout_receipt_verifies_under_the_merchant_key(wired: Gateway) -> None:
    checkout, _ = _buy(wired)
    receipt, claims = verify_and_load(
        checkout["checkout_receipt_jws"],
        wired.keyring,
        CheckoutReceiptContents,
        expected_role=ROLE_MERCHANT,
    )
    assert receipt.vct == VCT_CHECKOUT_RECEIPT
    assert receipt.amount == inr(1299)
    assert receipt.status == "confirmed"
    assert claims["iss"] == wired.merchant_signer.kid


def test_a_checkout_receipt_binds_to_both_mandates(wired: Gateway) -> None:
    """It names the merchant's signed cart *and* the buyer's authorisation.

    Either hash alone would leave a gap: the cart without the authorisation does
    not show the buyer agreed, and the authorisation without the cart does not
    show what they agreed to.
    """
    checkout, _ = _buy(wired)
    receipt = CheckoutReceiptContents.model_validate(checkout["checkout_receipt"])
    assert receipt.checkout_hash == checkout_hash(checkout["checkout_mandate_jwt"])
    assert receipt.open_checkout_mandate_hash == checkout_hash(wired.open_checkout_jws)


# ---------------------------------------------------------------------------
# Payment receipts
# ---------------------------------------------------------------------------


def test_a_payment_receipt_verifies_under_the_processor_key(wired: Gateway) -> None:
    _, paid = _buy(wired)
    receipt, claims = verify_and_load(
        paid["payment_receipt_jws"], wired.keyring, PaymentReceiptContents, expected_role=ROLE_MPP
    )
    assert receipt.vct == VCT_PAYMENT_RECEIPT
    assert receipt.status == "captured"
    assert receipt.amount == inr(1299)
    assert receipt.payment_id is not None
    assert receipt.order_id is not None
    assert claims["iss"] == wired.mpp.kid


def test_a_payment_receipt_binds_to_the_checkout_it_paid_for(wired: Gateway) -> None:
    """Without this, a receipt is just a number and cannot settle a dispute."""
    checkout, paid = _buy(wired)
    receipt = PaymentReceiptContents.model_validate(paid["payment_receipt"])
    assert receipt.checkout_hash == checkout_hash(checkout["checkout_mandate_jwt"])


def test_a_tampered_receipt_does_not_verify(wired: Gateway) -> None:
    _, paid = _buy(wired)
    jws = paid["payment_receipt_jws"]
    header, payload, signature = jws.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(MandateSignatureError):
        verify_and_load(f"{header}.{payload}.{flipped}", wired.keyring, PaymentReceiptContents)


def test_a_receipt_signed_by_the_merchant_is_not_a_processor_receipt(wired: Gateway) -> None:
    """Roles are separate for a reason: a shop cannot attest that it was paid."""
    _, paid = _buy(wired)
    receipt = PaymentReceiptContents.model_validate(paid["payment_receipt"])
    forged = wired.merchant_signer.sign(receipt, ttl_seconds=600)
    with pytest.raises(UntrustedIssuerError):
        verify_and_load(forged, wired.keyring, PaymentReceiptContents, expected_role=ROLE_MPP)


def test_receipts_are_long_lived(wired: Gateway) -> None:
    """A dispute is months later, so the receipt must still verify then."""
    from gateway.mandates import decode_unverified

    _, paid = _buy(wired)
    claims = decode_unverified(paid["payment_receipt_jws"])
    lifetime_days = (claims["exp"] - claims["iat"]) / 86400
    assert lifetime_days > 300


def test_the_receipt_ids_are_distinct_per_purchase(wired: Gateway) -> None:
    _, first = _buy(wired, "SF-RUN-001")
    _, second = _buy(wired, "SF-APP-001")
    assert first["payment_receipt"]["receipt_id"] != second["payment_receipt"]["receipt_id"]
    assert (
        first["payment_receipt"]["idempotency_key"] != second["payment_receipt"]["idempotency_key"]
    )


def test_a_failed_purchase_still_produces_a_verifiable_receipt(wired: Gateway) -> None:
    """A failure receipt is a contract that nothing was charged.

    Silence would leave an agent unable to distinguish "declined" from "lost in
    transit", which is precisely how double charges happen.
    """
    from gateway.razorpay_client import FakeRail

    assert isinstance(wired.rail, FakeRail)
    wired.rail.decline(times=None)  # every instrument declines

    _, paid = _buy(wired)
    receipt, _ = verify_and_load(
        paid["payment_receipt_jws"], wired.keyring, PaymentReceiptContents, expected_role=ROLE_MPP
    )
    assert receipt.status == "failed"
    assert receipt.failure_code == "recovery.attempts_exhausted"
    assert receipt.attempts == 3
    assert receipt.payment_id is None
    assert wired.rail.captured_total() == 0
    assert wired.ledger.total_captured() == 0


def test_the_receipt_is_reconcilable_against_the_ledger_and_the_rail(wired: Gateway) -> None:
    """Three independent records of the same money, which must agree."""
    from gateway.razorpay_client import FakeRail

    assert isinstance(wired.rail, FakeRail)
    _buy(wired, "SF-RUN-001")
    _buy(wired, "SF-APP-001")

    from gateway.audit import Event

    receipts_total = sum(
        int(row.payload["amount"])
        for row in wired.audit.rows(event=Event.PAYMENT_RECEIPT_ISSUED)
        if row.payload["status"] == "captured"
    )
    assert receipts_total == wired.ledger.total_captured() == wired.rail.captured_total()
    assert receipts_total == inr(1299) + inr(899)
