"""The Merchant Payment Processor.

The headline property: funds move only after ALLOW, and only once.
"""

from __future__ import annotations

import pytest

from ap2_min.models import CheckoutMandateContents, inr
from gateway.audit import AuditLog, Event
from gateway.ledger import Ledger
from gateway.mandates import KeyRing
from gateway.payments import (
    PaymentNotAuthorized,
    PaymentProcessor,
    idempotency_key,
)
from gateway.razorpay_client import (
    METHOD_CARD,
    METHOD_UPI,
    TEST_VPA_FAILURE,
    FakeRail,
    RailDeclined,
    RailTimeout,
)
from gateway.verify import Decision, Outcome, verify_payment_mandate

from .factories import MERCHANT_ID, Scenario

# ---------------------------------------------------------------------------
# The authorisation gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", [Outcome.DENY, Outcome.UNRESOLVED_CONSTRAINT])
def test_execute_payment_refuses_anything_but_allow(
    outcome: Outcome,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    """Not a warning. An exception, before the rail is touched."""
    refused = Decision(outcome=outcome, code="whatever", payment_mandate_id="pm_x", amount=inr(100))
    with pytest.raises(PaymentNotAuthorized, match="requires an ALLOW"):
        processor.execute_payment(refused, checkout_contents)
    assert rail.calls == [], "a non-ALLOW decision must not reach the payment rail"


def test_a_denied_mandate_never_reaches_the_rail(
    scenario: Scenario,
    keyring: KeyRing,
    ledger: Ledger,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    """Failure mode 3, end to end: rejected at the boundary, nothing downstream."""
    decision = verify_payment_mandate(
        "not-a-real-mandate", scenario.checkout_jws, ledger, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    with pytest.raises(PaymentNotAuthorized):
        processor.execute_payment(decision, checkout_contents)
    assert rail.calls == []
    assert rail.captured_total() == 0


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_an_allowed_payment_captures_and_issues_a_signed_receipt(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    keyring: KeyRing,
) -> None:
    outcome = processor.execute_payment(allow, checkout_contents)

    assert outcome.captured
    assert outcome.receipt.status == "captured"
    assert outcome.receipt.amount == inr(1299)
    assert outcome.receipt.payee == MERCHANT_ID
    assert outcome.receipt.payment_id is not None
    assert outcome.receipt.order_id is not None
    assert outcome.receipt.checkout_hash == allow.bound_checkout_hash
    assert not outcome.replayed
    assert rail.captured_total() == inr(1299)


def test_the_receipt_verifies_under_the_mpp_public_key(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    keyring: KeyRing,
) -> None:
    """A receipt is checkable by anyone with the public key — no database needed."""
    from ap2_min.models import PaymentReceiptContents
    from ap2_min.roles import ROLE_MPP
    from gateway.mandates import verify_and_load

    outcome = processor.execute_payment(allow, checkout_contents)
    parsed, claims = verify_and_load(
        outcome.receipt_jws, keyring, PaymentReceiptContents, expected_role=ROLE_MPP
    )
    assert parsed == outcome.receipt
    assert claims["iss"] == "key_mpp_1"


def test_a_capture_consumes_budget(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    ledger: Ledger,
) -> None:
    assert allow.open_mandate_id is not None
    assert ledger.spent_under(allow.open_mandate_id) == 0
    processor.execute_payment(allow, checkout_contents)
    assert ledger.spent_under(allow.open_mandate_id) == inr(1299)


def test_a_decline_does_not_consume_budget(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    ledger: Ledger,
    rail: FakeRail,
) -> None:
    """A hostile counterparty must not be able to exhaust a daily limit by declining.

    If declines ate budget, anyone who could make our payments fail could lock a
    user out of their own money for the rest of the day.
    """
    assert allow.payment_mandate_id is not None
    assert allow.open_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id)
    with pytest.raises(RailDeclined):
        processor.execute_payment(allow, checkout_contents)
    assert ledger.spent_under(allow.open_mandate_id) == 0
    assert ledger.total_captured() == 0


def test_the_order_receipt_carries_the_idempotency_key_for_dashboard_tracing(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    assert allow.payment_mandate_id is not None
    processor.execute_payment(allow, checkout_contents)
    order = rail.orders()[0]
    assert order.receipt == idempotency_key(allow.payment_mandate_id)[:40]
    assert order.notes["reference"] == allow.payment_mandate_id
    assert order.notes["protocol"] == "ap2-v0.2"


def test_the_nonce_is_burned_on_first_use(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    ledger: Ledger,
) -> None:
    assert allow.nonce is not None
    assert ledger.nonce_owner(allow.nonce) is None
    processor.execute_payment(allow, checkout_contents)
    assert ledger.nonce_owner(allow.nonce) == allow.payment_mandate_id


# ---------------------------------------------------------------------------
# Rail failures propagate as themselves
# ---------------------------------------------------------------------------


def test_a_decline_propagates_as_raildeclined(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    """Interpreting a decline is recovery's job, not the processor's."""
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id)
    with pytest.raises(RailDeclined):
        processor.execute_payment(allow, checkout_contents)


def test_a_timeout_propagates_as_railtimeout(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    assert allow.payment_mandate_id is not None
    rail.timeout(reference=allow.payment_mandate_id)
    with pytest.raises(RailTimeout):
        processor.execute_payment(allow, checkout_contents)


def test_a_failing_test_vpa_declines_exactly_like_the_real_sandbox(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
) -> None:
    """FakeRail honours failure@razorpay, so the offline path mirrors the live one."""
    with pytest.raises(RailDeclined):
        processor.execute_payment(allow, checkout_contents, method=METHOD_UPI, vpa=TEST_VPA_FAILURE)


# ---------------------------------------------------------------------------
# Failure receipts
# ---------------------------------------------------------------------------


def test_a_failure_gets_a_signed_receipt_too(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    keyring: KeyRing,
) -> None:
    """Silence is worse than "no". A failed payment is a contract that nothing moved."""
    outcome = processor.finalise_failure(
        allow,
        checkout_contents,
        failure_code="recovery.attempts_exhausted",
        failure_reason="the bank declined every instrument",
        attempts=3,
        method=METHOD_CARD,
    )
    assert not outcome.captured
    assert outcome.receipt.status == "failed"
    assert outcome.receipt.failure_code == "recovery.attempts_exhausted"
    assert outcome.receipt.attempts == 3
    assert outcome.receipt.payment_id is None
    assert outcome.receipt_jws


def test_a_failed_receipt_without_a_code_cannot_be_constructed() -> None:
    """The model refuses an unexplained failure."""
    from datetime import UTC, datetime

    from ap2_min.models import PaymentReceiptContents

    with pytest.raises(ValueError, match="failure_code"):
        PaymentReceiptContents(
            receipt_id="r1",
            status="failed",
            payment_mandate_id="pm",
            idempotency_key="k",
            amount=1,
            payee="m",
            checkout_hash="h",
            ts=datetime.now(UTC),
        )


def test_a_captured_receipt_without_a_payment_id_cannot_be_constructed() -> None:
    from datetime import UTC, datetime

    from ap2_min.models import PaymentReceiptContents

    with pytest.raises(ValueError, match="payment_id"):
        PaymentReceiptContents(
            receipt_id="r1",
            status="captured",
            payment_mandate_id="pm",
            idempotency_key="k",
            amount=1,
            payee="m",
            checkout_hash="h",
            ts=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# Audit coverage
# ---------------------------------------------------------------------------


def test_every_step_of_a_payment_is_audited(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    audit: AuditLog,
) -> None:
    processor.execute_payment(allow, checkout_contents)
    events = [row.event for row in audit.rows()]
    assert Event.ORDER_CREATED in events
    assert Event.PAYMENT_ATTEMPT in events
    assert Event.PAYMENT_CAPTURED in events
    assert Event.PAYMENT_RECEIPT_ISSUED in events
    assert audit.verify_chain().ok


def test_every_audited_money_action_carries_a_human_reason(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    audit: AuditLog,
) -> None:
    """ "Every money action explainable" — checked, not asserted in a README."""
    processor.execute_payment(allow, checkout_contents)
    for row in audit.rows():
        assert row.human_reason, f"{row.event} has no explanation"
        assert len(row.human_reason) > 10


def test_a_declined_payment_is_audited_with_its_reason(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    audit: AuditLog,
) -> None:
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id)
    with pytest.raises(RailDeclined):
        processor.execute_payment(allow, checkout_contents)
    declines = audit.rows(event=Event.PAYMENT_DECLINED)
    assert len(declines) == 1
    assert declines[0].payload["error"] == "rail.declined"
    assert "No money moved" in (declines[0].human_reason or "")
