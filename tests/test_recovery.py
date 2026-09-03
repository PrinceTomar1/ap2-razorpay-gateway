"""Bounded recovery and the circuit breaker.

The bound is the point. A retry loop without an explicit stopping rule is not
resilience, it is a way to turn one failure into many.
"""

from __future__ import annotations

import pytest

from ap2_min.models import CheckoutMandateContents, inr
from gateway.audit import AuditLog, Event
from gateway.ledger import Ledger
from gateway.mandates import Signer
from gateway.payments import PaymentProcessor, idempotency_key
from gateway.policy import CircuitBreakerPolicy, Policy, RecoveryPolicy
from gateway.razorpay_client import (
    METHOD_CARD,
    METHOD_PAYMENT_LINK,
    METHOD_UPI,
    FakeRail,
)
from gateway.recovery import (
    BreakerState,
    CircuitBreaker,
    FailureCode,
    RecoveryPlaybook,
)
from gateway.verify import Decision

# ---------------------------------------------------------------------------
# The instrument ladder
# ---------------------------------------------------------------------------


def test_the_mandates_own_instrument_is_tried_first(playbook: RecoveryPlaybook) -> None:
    """The user chose it. We do not get to reorder that."""
    assert playbook.instrument_order(METHOD_CARD) == [METHOD_CARD, METHOD_UPI, METHOD_PAYMENT_LINK]


def test_the_ladder_is_deduplicated(playbook: RecoveryPlaybook) -> None:
    assert playbook.instrument_order(METHOD_UPI) == [METHOD_UPI, METHOD_PAYMENT_LINK, METHOD_CARD]


def test_the_ladder_is_capped_by_max_attempts(
    processor: PaymentProcessor, audit: AuditLog, breaker: CircuitBreaker
) -> None:
    """The stopping rule is visible before the first attempt, not discovered in a loop."""
    strict = RecoveryPlaybook(
        processor=processor,
        policy=RecoveryPolicy(
            max_attempts=2,
            method_fallback=[METHOD_UPI, METHOD_PAYMENT_LINK, METHOD_CARD],
            backoff_base_seconds=0.0,
            backoff_factor=2.0,
            backoff_max_seconds=8.0,
        ),
        breaker=breaker,
        audit=audit,
        sleep=lambda _s: None,
    )
    assert strict.instrument_order(METHOD_UPI) == [METHOD_UPI, METHOD_PAYMENT_LINK]


# ---------------------------------------------------------------------------
# Recovery that succeeds
# ---------------------------------------------------------------------------


def test_a_first_attempt_success_is_not_counted_as_recovery(
    allow: Decision, playbook: RecoveryPlaybook, checkout_contents: CheckoutMandateContents
) -> None:
    result = playbook.run(allow, checkout_contents)
    assert result.captured
    assert not result.recovered
    assert result.attempts == 1
    assert result.methods_tried == (METHOD_UPI,)


def test_a_decline_falls_back_to_the_next_instrument_and_succeeds(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    audit: AuditLog,
) -> None:
    """Failure mode 1, in full."""
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id, methods={METHOD_UPI})

    result = playbook.run(allow, checkout_contents)

    assert result.captured
    assert result.recovered
    assert result.attempts == 2
    assert result.methods_tried == (METHOD_UPI, METHOD_PAYMENT_LINK)
    assert result.outcome is not None
    assert result.outcome.receipt.method == METHOD_PAYMENT_LINK
    assert result.outcome.receipt.attempts == 2
    assert rail.captured_total() == inr(1299), "recovered, and charged exactly once"

    fallbacks = audit.rows(event=Event.RECOVERY_METHOD_FALLBACK)
    assert len(fallbacks) == 1
    assert fallbacks[0].payload["failed_method"] == METHOD_UPI
    assert fallbacks[0].payload["next_method"] == METHOD_PAYMENT_LINK
    assert audit.rows(event=Event.RECOVERY_SUCCEEDED)


def test_recovery_walks_the_whole_ladder_before_giving_up(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id, times=2)
    result = playbook.run(allow, checkout_contents)
    assert result.captured
    assert result.attempts == 3
    assert result.methods_tried == (METHOD_UPI, METHOD_PAYMENT_LINK, METHOD_CARD)


# ---------------------------------------------------------------------------
# Recovery that stops
# ---------------------------------------------------------------------------


def test_exhaustion_stops_at_max_attempts_and_issues_a_failure_receipt(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    audit: AuditLog,
    policy: Policy,
) -> None:
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id, times=None)  # decline forever

    result = playbook.run(allow, checkout_contents)

    assert not result.captured
    assert result.attempts == policy.recovery.max_attempts
    assert result.outcome is not None
    assert result.outcome.receipt.status == "failed"
    assert result.outcome.receipt.failure_code == FailureCode.EXHAUSTED
    assert result.outcome.receipt.attempts == 3
    assert rail.captured_total() == 0

    exhausted = audit.rows(event=Event.RECOVERY_EXHAUSTED)
    assert len(exhausted) == 1
    assert exhausted[0].payload["max_attempts"] == 3
    assert exhausted[0].payload["methods_tried"] == [METHOD_UPI, METHOD_PAYMENT_LINK, METHOD_CARD]


def test_a_never_ending_decline_cannot_produce_more_than_max_attempts_orders(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    """The bound holds even against an adversarial rail."""
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id, times=None)
    playbook.run(allow, checkout_contents)
    assert len(rail.orders()) == 3


def test_stock_selling_out_mid_recovery_stops_before_the_next_attempt(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    audit: AuditLog,
) -> None:
    """Recovery must never buy something that sold out while we were retrying."""
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id, methods={METHOD_UPI})
    calls = {"n": 0}

    def stock_check() -> tuple[bool, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return True, "in stock"
        return False, "SF-RUN-001 sold out while the payment was being retried"

    result = playbook.run(allow, checkout_contents, stock_check=stock_check)

    assert not result.captured
    assert result.outcome is not None
    assert result.outcome.receipt.failure_code == FailureCode.STOCK_UNAVAILABLE
    assert rail.captured_total() == 0
    assert len(rail.orders()) == 1, "no second order once stock was gone"
    assert audit.rows(event=Event.STOCK_RECHECK_FAILED)


def test_stock_gone_before_the_first_attempt_never_touches_the_rail(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    result = playbook.run(
        allow, checkout_contents, stock_check=lambda: (False, "sold out before we started")
    )
    assert not result.captured
    assert rail.orders() == []
    assert rail.captured_total() == 0


# ---------------------------------------------------------------------------
# The circuit breaker. Failure mode 2.
# ---------------------------------------------------------------------------


def test_declines_do_not_trip_the_breaker(breaker: CircuitBreaker) -> None:
    """A decline is the rail working correctly and saying no."""
    for _ in range(20):
        breaker.record_decline()
    assert breaker.state is BreakerState.CLOSED


def test_consecutive_transport_failures_trip_the_breaker(breaker: CircuitBreaker) -> None:
    assert breaker.record_transport_failure() is False
    assert breaker.record_transport_failure() is True
    assert breaker.state is BreakerState.OPEN
    assert breaker.is_open()


def test_a_success_closes_the_breaker(breaker: CircuitBreaker) -> None:
    breaker.record_transport_failure()
    breaker.record_transport_failure()
    assert breaker.is_open()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_the_breaker_goes_half_open_after_its_reset_window() -> None:
    now = {"t": 0.0}
    breaker = CircuitBreaker(
        CircuitBreakerPolicy(failure_threshold=1, reset_after_seconds=30),
        clock=lambda: now["t"],
    )
    breaker.record_transport_failure()
    assert breaker.state.value == "open"
    now["t"] = 31.0
    assert breaker.state.value == "half_open"
    assert not breaker.is_open(), "a half-open breaker lets one probe through"


def test_a_timeout_storm_opens_the_breaker_and_defers_without_a_receipt(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    audit: AuditLog,
    ledger: Ledger,
) -> None:
    """Failure mode 2: the mandate is left UNSPENT, and the agent is told to return."""
    assert allow.payment_mandate_id is not None
    rail.timeout(reference=allow.payment_mandate_id, times=None)

    result = playbook.run(allow, checkout_contents)

    assert result.deferred
    assert result.outcome is None, "a deferral issues no receipt — the mandate is still good"
    assert rail.captured_total() == 0

    key = idempotency_key(allow.payment_mandate_id)
    record = ledger.get_idempotency(key)
    assert record is not None
    assert record.status == "in_flight", "not terminal: the mandate remains presentable"

    assert audit.rows(event=Event.CIRCUIT_OPENED)
    deferred = audit.rows(event=Event.CIRCUIT_DEFERRED)
    assert len(deferred) == 1
    assert deferred[0].payload["mandate_spent"] is False


def test_an_open_breaker_defers_before_touching_the_rail(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    breaker: CircuitBreaker,
    audit: AuditLog,
) -> None:
    breaker.record_transport_failure()
    breaker.record_transport_failure()
    assert breaker.is_open()

    result = playbook.run(allow, checkout_contents)

    assert result.deferred
    assert result.attempts == 0
    assert rail.calls == [], "an open breaker means the rail is not contacted at all"
    assert audit.rows(event=Event.CIRCUIT_DEFERRED)


def test_a_deferred_mandate_can_be_retried_on_the_next_tick(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    breaker: CircuitBreaker,
) -> None:
    """The whole reason a deferral issues no receipt.

    Tick one: the rail is down, we defer. Tick two: the rail is back, and the very
    same mandate goes through. Once.
    """
    assert allow.payment_mandate_id is not None
    rail.timeout(reference=allow.payment_mandate_id, times=None)
    assert playbook.run(allow, checkout_contents).deferred

    rail.reset_rules()
    breaker.record_success()

    second = playbook.run(allow, checkout_contents)
    assert second.captured
    assert rail.captured_total() == inr(1299)


def test_a_deferred_attempt_that_actually_captured_is_found_on_the_next_tick(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    breaker: CircuitBreaker,
    ledger: Ledger,
    audit: AuditLog,
) -> None:
    """Timeout, then the payment lands anyway, then we come back. No double charge."""
    assert allow.payment_mandate_id is not None
    rail.timeout(reference=allow.payment_mandate_id, times=None)
    assert playbook.run(allow, checkout_contents).deferred

    record = ledger.get_idempotency(idempotency_key(allow.payment_mandate_id))
    assert record is not None
    orphan = record.order_ids[0]
    rail.reset_rules()
    rail.complete_test_payment(orphan, method=METHOD_UPI)  # it went through after all
    breaker.record_success()

    result = playbook.run(allow, checkout_contents)
    assert result.captured
    assert result.outcome is not None
    assert result.outcome.receipt.order_id == orphan
    assert rail.captured_total() == inr(1299), "one capture, not two"
    assert audit.rows(event=Event.RECOVERY_ABORTED_PRIOR_CAPTURE)


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_backoff_is_exponential_and_capped() -> None:
    policy = RecoveryPolicy(
        max_attempts=3,
        method_fallback=["a", "b", "c"],
        backoff_base_seconds=1.0,
        backoff_factor=2.0,
        backoff_max_seconds=3.0,
    )
    assert policy.backoff_for(0) == 1.0
    assert policy.backoff_for(1) == 2.0
    assert policy.backoff_for(2) == 3.0, "capped"
    assert policy.backoff_for(9) == 3.0


def test_zero_backoff_never_sleeps(playbook: RecoveryPlaybook) -> None:
    """The shipped policy uses 0.0 so the demo and the suite do not sit idle."""
    assert playbook.policy.backoff_for(0) == 0.0
    assert playbook.policy.backoff_for(5) == 0.0


def test_backoff_is_applied_between_attempts_but_not_before_the_first(
    allow: Decision,
    processor: PaymentProcessor,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    audit: AuditLog,
    breaker: CircuitBreaker,
) -> None:
    assert allow.payment_mandate_id is not None
    slept: list[float] = []
    slow = RecoveryPlaybook(
        processor=processor,
        policy=RecoveryPolicy(
            max_attempts=3,
            method_fallback=[METHOD_UPI, METHOD_PAYMENT_LINK, METHOD_CARD],
            backoff_base_seconds=0.5,
            backoff_factor=2.0,
            backoff_max_seconds=8.0,
        ),
        breaker=breaker,
        audit=audit,
        sleep=slept.append,
    )
    rail.decline(reference=allow.payment_mandate_id, times=2)
    slow.run(allow, checkout_contents)
    assert slept == [0.5, 1.0], "no sleep before attempt 1; exponential thereafter"


# ---------------------------------------------------------------------------
# Auditing
# ---------------------------------------------------------------------------


def test_the_bound_is_announced_before_the_first_attempt(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    audit: AuditLog,
) -> None:
    playbook.run(allow, checkout_contents)
    started = audit.rows(event=Event.RECOVERY_STARTED)
    assert len(started) == 1
    assert started[0].payload["max_attempts"] == 3
    assert started[0].payload["instrument_order"] == [METHOD_UPI, METHOD_PAYMENT_LINK, METHOD_CARD]


def test_the_audit_chain_survives_a_full_recovery_run(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    audit: AuditLog,
) -> None:
    assert allow.payment_mandate_id is not None
    rail.decline(reference=allow.payment_mandate_id, times=None)
    playbook.run(allow, checkout_contents)
    assert audit.verify_chain().ok
    for row in audit.rows():
        assert row.human_reason


def test_recovery_result_serialises_for_the_demo_report(
    allow: Decision, playbook: RecoveryPlaybook, checkout_contents: CheckoutMandateContents
) -> None:
    body = playbook.run(allow, checkout_contents).as_dict()
    assert body["captured"] is True
    assert body["recovered"] is False
    assert body["attempts"] == 1
    assert body["receipt"]["status"] == "captured"


def test_recovery_uses_the_processor_and_never_charges_without_allow(
    processor: PaymentProcessor,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
) -> None:
    from gateway.payments import PaymentNotAuthorized
    from gateway.verify import Outcome

    denied = Decision(outcome=Outcome.DENY, code="nope", payment_mandate_id="pm", amount=1)
    with pytest.raises(PaymentNotAuthorized):
        playbook.run(denied, checkout_contents)
    assert rail.calls == []


def test_a_signer_is_not_needed_to_read_a_deferral(
    allow: Decision,
    playbook: RecoveryPlaybook,
    checkout_contents: CheckoutMandateContents,
    rail: FakeRail,
    mpp_signer: Signer,
) -> None:
    """A deferral is a plain object, not a signed artefact — nothing was decided."""
    assert allow.payment_mandate_id is not None
    rail.timeout(reference=allow.payment_mandate_id, times=None)
    result = playbook.run(allow, checkout_contents)
    assert result.deferred
    assert "has not been spent" in result.human_reason
