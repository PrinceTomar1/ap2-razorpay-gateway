"""Failure mode 6: one Payment Mandate, at most one charge. Ever.

This is the property a payments system is judged on, so it gets its own file and
is attacked from every direction: naive duplicate submit, concurrent submit,
retry after a decline, retry after a timeout that actually succeeded, and a
mandate re-presented after its recovery run was exhausted.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from ap2_min.models import CheckoutMandateContents, inr
from gateway.audit import AuditLog, Event
from gateway.ledger import DoubleFinalisationError, Ledger
from gateway.mandates import KeyRing, Signer
from gateway.payments import ConcurrentAttemptError, PaymentProcessor, idempotency_key
from gateway.razorpay_client import METHOD_CARD, METHOD_UPI, FakeRail, RailDeclined, RailTimeout
from gateway.recovery import RecoveryPlaybook
from gateway.verify import Decision, Outcome, verify_payment_mandate

from .factories import Scenario

# ---------------------------------------------------------------------------
# The key itself
# ---------------------------------------------------------------------------


def test_the_key_is_sha256_of_the_mandate_id() -> None:
    import hashlib

    assert idempotency_key("pm_abc") == hashlib.sha256(b"pm_abc").hexdigest()
    assert len(idempotency_key("pm_abc")) == 64


def test_different_mandates_get_different_keys() -> None:
    assert idempotency_key("pm_a") != idempotency_key("pm_b")


# ---------------------------------------------------------------------------
# Duplicate submit
# ---------------------------------------------------------------------------


def test_the_same_mandate_presented_twice_charges_once(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    ledger: Ledger,
) -> None:
    """The headline assertion of failure mode 6."""
    first = processor.execute_payment(allow, checkout_contents)
    second = processor.execute_payment(allow, checkout_contents)

    assert first.captured and second.captured
    assert second.replayed
    assert not first.replayed
    assert second.receipt.receipt_id == first.receipt.receipt_id
    assert second.receipt.payment_id == first.receipt.payment_id
    assert second.receipt_jws == first.receipt_jws

    assert rail.captured_total() == inr(1299), "the rail must have captured exactly once"
    assert len(rail.orders()) == 1, "a second order must never be created"
    assert ledger.total_captured() == inr(1299)


def test_presenting_the_same_mandate_ten_times_still_charges_once(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    receipts = [processor.execute_payment(allow, checkout_contents) for _ in range(10)]
    assert len({r.receipt.receipt_id for r in receipts}) == 1
    assert rail.captured_total() == inr(1299)
    assert len(rail.orders()) == 1


def test_concurrent_presentations_charge_once(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    """Eight threads racing on the same mandate.

    The stored receipt alone cannot save us here: at t=0 all eight read "no
    receipt yet". Only the attempt lease serialises them, and the loser threads
    then find the winner's receipt.
    """
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(lambda _: processor.execute_payment(allow, checkout_contents), range(8))
        )
    assert rail.captured_total() == inr(1299)
    assert len(rail.orders()) == 1, "eight simultaneous requests, one order"
    assert len({o.receipt.receipt_id for o in outcomes}) == 1
    assert sum(1 for o in outcomes if not o.replayed) == 1, "exactly one did the work"


def test_the_attempt_lease_is_exclusive(ledger: Ledger) -> None:
    ledger.claim("k1", "pm_1")
    assert ledger.acquire_attempt_lease("k1", lease_seconds=60) is True
    assert ledger.acquire_attempt_lease("k1", lease_seconds=60) is False
    ledger.release_attempt_lease("k1")
    assert ledger.acquire_attempt_lease("k1", lease_seconds=60) is True


def test_an_expired_lease_can_be_taken_over(ledger: Ledger) -> None:
    """A process that crashes mid-attempt must not wedge the mandate forever.

    The successor still runs the capture probe, so taking over is safe.
    """
    ledger.claim("k1", "pm_1")
    assert ledger.acquire_attempt_lease("k1", lease_seconds=-1) is True  # already expired
    assert ledger.acquire_attempt_lease("k1", lease_seconds=60) is True


def test_a_settled_key_grants_no_lease(ledger: Ledger) -> None:
    ledger.claim("k1", "pm_1")
    ledger.finalise("k1", status="captured", receipt_jws="j", receipt={"receipt_id": "r"})
    assert ledger.acquire_attempt_lease("k1", lease_seconds=60) is False


def test_a_stuck_concurrent_attempt_errors_rather_than_guessing(
    allow: Decision,
    rail: FakeRail,
    ledger: Ledger,
    audit: AuditLog,
    mpp_signer: Signer,
    checkout_contents: CheckoutMandateContents,
) -> None:
    """We refuse rather than start a second attempt whose predecessor is unresolved.

    Waiting forever is not an option and neither is charging twice, so the honest
    third answer is to raise.
    """
    assert allow.payment_mandate_id is not None
    impatient = PaymentProcessor(
        rail=rail,
        ledger=ledger,
        audit=audit,
        signer=mpp_signer,
        concurrent_wait_seconds=0.0,
        sleep=lambda _s: None,
    )
    key = idempotency_key(allow.payment_mandate_id)
    ledger.claim(key, allow.payment_mandate_id)
    ledger.acquire_attempt_lease(key, lease_seconds=600)

    with pytest.raises(ConcurrentAttemptError, match="held the lease"):
        impatient.execute_payment(allow, checkout_contents)
    assert rail.calls == []


def test_a_failed_receipt_is_also_idempotent(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    """Re-presenting a mandate that already failed returns the failure, not a retry.

    Otherwise a client that retries on error would eventually get through — which
    would make the attempt cap meaningless.
    """
    processor.finalise_failure(
        allow,
        checkout_contents,
        failure_code="recovery.attempts_exhausted",
        failure_reason="declined everywhere",
        attempts=3,
    )
    replay = processor.execute_payment(allow, checkout_contents)
    assert replay.replayed
    assert replay.receipt.status == "failed"
    assert rail.calls == [], "a settled mandate must not reach the rail again"


def test_existing_outcome_finds_a_settled_mandate(
    allow: Decision, processor: PaymentProcessor, checkout_contents: CheckoutMandateContents
) -> None:
    assert allow.payment_mandate_id is not None
    assert processor.existing_outcome(allow.payment_mandate_id) is None
    processor.execute_payment(allow, checkout_contents)
    found = processor.existing_outcome(allow.payment_mandate_id)
    assert found is not None
    assert found.replayed
    assert found.captured


# ---------------------------------------------------------------------------
# Retry safety: the dangerous cases
# ---------------------------------------------------------------------------


def test_a_retry_after_a_decline_creates_a_new_order_but_charges_once(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id, methods={METHOD_UPI})

    with pytest.raises(RailDeclined):
        processor.execute_payment(allow, checkout_contents, method=METHOD_UPI)
    outcome = processor.execute_payment(allow, checkout_contents, method=METHOD_CARD)

    assert outcome.captured
    assert len(rail.orders()) == 2, "a declined order is dead; the retry needs its own"
    assert rail.captured_total() == inr(1299), "but only one of them captured"


def test_a_retry_after_a_timeout_that_actually_captured_does_not_double_charge(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    ledger: Ledger,
    audit: AuditLog,
) -> None:
    """The nastiest case in payments, and the reason for the capture probe.

    We time out (so the caller learns nothing), then arrange for that order to
    have captured after all, then retry. The probe must find the capture and stop.
    """
    assert allow.payment_mandate_id is not None
    key = idempotency_key(allow.payment_mandate_id)

    rail.timeout(reference=allow.payment_mandate_id, methods={METHOD_UPI})
    with pytest.raises(RailTimeout):
        processor.execute_payment(allow, checkout_contents, method=METHOD_UPI)

    # The order exists and, unknown to us, it went through.
    record = ledger.get_idempotency(key)
    assert record is not None
    assert len(record.order_ids) == 1
    orphaned_order = record.order_ids[0]
    rail.complete_test_payment(orphaned_order, method=METHOD_UPI)

    # Now retry, as recovery would.
    outcome = processor.execute_payment(allow, checkout_contents, method=METHOD_CARD)

    assert outcome.captured
    assert outcome.receipt.order_id == orphaned_order, "must settle the order that really paid"
    assert rail.captured_total() == inr(1299), "exactly one capture"
    assert len(rail.orders()) == 1, "the probe must stop before a second order is created"
    assert audit.rows(event=Event.RECOVERY_ABORTED_PRIOR_CAPTURE)


def test_recovery_never_double_charges_across_a_whole_playbook_run(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id, methods={METHOD_UPI})
    result = playbook.run(allow, checkout_contents)
    assert result.captured
    assert result.recovered
    assert rail.captured_total() == inr(1299)


def test_the_idempotency_root_survives_the_whole_recovery_run(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    ledger: Ledger,
) -> None:
    """Every attempt shares one root — that is what makes retry safe."""
    assert allow.payment_mandate_id is not None
    key = idempotency_key(allow.payment_mandate_id)
    rail.decline(reference=allow.payment_mandate_id, methods={METHOD_UPI, "payment_link"}, times=2)

    result = playbook.run(allow, checkout_contents)
    assert result.captured

    record = ledger.get_idempotency(key)
    assert record is not None
    assert record.status == "captured"
    assert len(record.order_ids) == 3, "three attempts, three orders, one key"
    assert all(order.receipt == key[:40] for order in rail.orders())
    assert rail.captured_total() == inr(1299)


# ---------------------------------------------------------------------------
# The store's own guarantees
# ---------------------------------------------------------------------------


def test_finalising_a_settled_key_twice_is_loud(ledger: Ledger) -> None:
    """Not a race to smooth over — this is the double charge, and it should shout."""
    ledger.claim("k1", "pm_1")
    ledger.finalise("k1", status="captured", receipt_jws="jws", receipt={"receipt_id": "r1"})
    with pytest.raises(DoubleFinalisationError, match="already terminal"):
        ledger.finalise("k1", status="captured", receipt_jws="jws2", receipt={"receipt_id": "r2"})


def test_claiming_the_same_key_twice_returns_the_same_record(ledger: Ledger) -> None:
    first = ledger.claim("k1", "pm_1")
    second = ledger.claim("k1", "pm_1")
    assert first == second
    assert first.status == "in_flight"


def test_a_non_terminal_status_is_refused(ledger: Ledger) -> None:
    ledger.claim("k1", "pm_1")
    with pytest.raises(ValueError, match="not a terminal status"):
        ledger.finalise("k1", status="in_flight", receipt_jws="x", receipt={})


def test_noting_an_unclaimed_key_raises(ledger: Ledger) -> None:
    with pytest.raises(KeyError):
        ledger.note_order("never_claimed", "order_1")


def test_noting_the_same_order_twice_does_not_inflate_the_attempt_count(
    ledger: Ledger,
) -> None:
    ledger.claim("k1", "pm_1")
    ledger.note_order("k1", "order_1")
    ledger.note_order("k1", "order_1")
    record = ledger.get_idempotency("k1")
    assert record is not None
    assert record.attempts == 1
    assert record.order_ids == ["order_1"]


def test_recording_the_same_spend_twice_counts_once(ledger: Ledger) -> None:
    """The spend ledger is keyed on the payment mandate, so it self-deduplicates."""
    for _ in range(3):
        ledger.record_spend(
            open_mandate_id="pmo_1",
            payment_mandate_id="pm_1",
            amount=inr(500),
            currency="INR",
            payee="m_stridefit",
        )
    assert ledger.spent_under("pmo_1") == inr(500)


# ---------------------------------------------------------------------------
# Replay vs idempotency: different questions, different answers
# ---------------------------------------------------------------------------


def test_a_different_mandate_reusing_a_burned_nonce_is_a_replay(
    scenario: Scenario, keyring: KeyRing, ledger: Ledger
) -> None:
    ledger.burn_nonce("shared_nonce_value", "pm_the_first_one")
    decision = verify_payment_mandate(
        scenario.present(nonce="shared_nonce_value", mandate_id="pm_a_different_one"),
        scenario.checkout_jws,
        ledger,
        keyring=keyring,
        now=scenario.now,
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == "payment.nonce.replayed"


def test_the_same_mandate_re_presenting_its_own_nonce_is_not_a_replay(
    scenario: Scenario, keyring: KeyRing, ledger: Ledger
) -> None:
    """Otherwise a deferred payment could never be retried on the next tick."""
    token = scenario.present(nonce="my_own_nonce", mandate_id="pm_mine")
    ledger.burn_nonce("my_own_nonce", "pm_mine")
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.ALLOW
