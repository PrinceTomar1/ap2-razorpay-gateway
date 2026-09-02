"""Bounded recovery: what happens when the rail says no, or says nothing.

A payment failing is not an exception in a payments system; it is the common
case. What matters is that the response is *bounded* — a fixed number of attempts,
a fixed set of instruments, a fixed stopping rule, and a signed receipt at the
end whichever way it goes.

The playbook, in full:

1. If the circuit breaker is open, stop before touching the rail. Issue **no**
   receipt: the mandate stays unspent and presentable on the next tick.
2. Before every attempt, re-check stock and price. Recovery must never buy
   something that sold out or changed price while we were retrying.
3. Attempt the payment on the current instrument.
4. On a **decline**, fall back to the next instrument. A decline is a definite
   answer, so retrying the same instrument would get the same answer.
5. On a **timeout or transport failure**, count it against the breaker. If the
   breaker trips, defer (as in 1). Otherwise fall back.
6. After ``recovery.max_attempts`` attempts, stop and issue a signed
   ``payment_failed`` receipt naming the reason.

What recovery is **not** allowed to change: the amount, the payee, the cart, or
the idempotency key. It moves down a list of instruments and nothing else. Every
attempt runs under ``sha256(payment_mandate.id)``, and
:class:`~gateway.payments.PaymentProcessor` probes every order already created
under that key before making another — so no path through this module can charge
twice, including the path where a timed-out attempt actually succeeded.

No language model here either. "Should we try again, and on what?" is a policy
question answered by config/policy.yaml, not a judgement call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ap2_min.models import CheckoutMandateContents, paise_to_inr_str
from ap2_min.roles import ROLE_MPP
from gateway.audit import AuditLog, Event
from gateway.payments import PaymentOutcome, PaymentProcessor
from gateway.policy import CircuitBreakerPolicy, RecoveryPolicy
from gateway.razorpay_client import RailDeclined, RailError, RailTimeout, RailUnavailable
from gateway.verify import Decision

#: ``() -> (ok, reason)``. Asked before every attempt. The merchant supplies one
#: that re-reads live stock and price; tests supply one that always says yes.
StockCheck = Callable[[], tuple[bool, str]]


class FailureCode:
    """Terminal reasons a payment can end without a capture."""

    EXHAUSTED = "recovery.attempts_exhausted"
    STOCK_UNAVAILABLE = "stock.unavailable"
    PRICE_CHANGED = "price.changed"
    RAIL_UNAVAILABLE = "rail.unavailable"


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Trips after N consecutive *transport* failures. Declines do not count.

    The distinction is the whole point. A declined card is the rail working
    correctly and telling us no; hammering it is rude but harmless. A rail that
    times out is a rail whose state we cannot observe, and every additional
    request against it is another payment that might have gone through without us
    knowing. So the breaker counts only the second kind.

    After ``reset_after_seconds`` the breaker goes half-open and lets one probe
    through. A success closes it; a failure re-opens it.
    """

    def __init__(
        self,
        policy: CircuitBreakerPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self._clock = clock
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self.policy.reset_after_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def is_open(self) -> bool:
        return self.state is BreakerState.OPEN

    def record_transport_failure(self) -> bool:
        """Count a timeout or unreachable rail. Returns True if this tripped it."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.policy.failure_threshold and self._opened_at is None:
            self._opened_at = self._clock()
            return True
        return False

    def record_decline(self) -> None:
        """A decline is a healthy rail. It resets the consecutive-failure run."""
        self._consecutive_failures = 0

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
            "threshold": self.policy.failure_threshold,
        }


@dataclass(frozen=True)
class RecoveryResult:
    """What the playbook concluded.

    ``deferred`` is the interesting field: it means no receipt was issued *on
    purpose*, because the mandate is still good and the rail is not. The agent is
    told to come back, not that it failed.
    """

    outcome: PaymentOutcome | None
    attempts: int
    methods_tried: tuple[str, ...]
    deferred: bool = False
    human_reason: str = ""

    @property
    def captured(self) -> bool:
        return self.outcome is not None and self.outcome.captured

    @property
    def recovered(self) -> bool:
        """Captured, but not on the first attempt. This is the demo's ``recovered`` count."""
        return self.captured and self.attempts > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "captured": self.captured,
            "recovered": self.recovered,
            "deferred": self.deferred,
            "attempts": self.attempts,
            "methods_tried": list(self.methods_tried),
            "human_reason": self.human_reason,
            "receipt": self.outcome.receipt.model_dump(mode="json") if self.outcome else None,
        }


def _always_available() -> tuple[bool, str]:
    return True, "no stock check configured"


class RecoveryPlaybook:
    """Runs a payment with bounded retries. The stopping rule is explicit."""

    def __init__(
        self,
        *,
        processor: PaymentProcessor,
        policy: RecoveryPolicy,
        breaker: CircuitBreaker,
        audit: AuditLog,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.processor = processor
        self.policy = policy
        self.breaker = breaker
        self.audit = audit
        self._sleep = sleep

    def instrument_order(self, preferred: str | None) -> list[str]:
        """The instruments to try, in order, capped at ``max_attempts``.

        The mandate's own instrument goes first — the user chose it — then the
        policy fallback list, de-duplicated. Capping here rather than inside the
        loop means the bound is visible at the top of the run and appears in the
        audit log before the first attempt.
        """
        ordered: list[str] = []
        for method in [preferred, *self.policy.method_fallback]:
            if method and method not in ordered:
                ordered.append(method)
        return ordered[: self.policy.max_attempts]

    def run(
        self,
        decision: Decision,
        checkout: CheckoutMandateContents,
        *,
        stock_check: StockCheck | None = None,
    ) -> RecoveryResult:
        """Execute the payment, recovering within bounds. Always terminal or deferred."""
        check = stock_check or _always_available
        methods = self.instrument_order(decision.payment_instrument)
        tried: list[str] = []

        self.audit.append(
            ROLE_MPP,
            Event.RECOVERY_STARTED,
            {
                "payment_mandate_id": decision.payment_mandate_id,
                "amount": decision.amount,
                "max_attempts": self.policy.max_attempts,
                "instrument_order": methods,
                "breaker": self.breaker.snapshot(),
            },
            f"Paying ₹{paise_to_inr_str(decision.amount or 0)} with at most "
            f"{self.policy.max_attempts} attempt(s), trying {' then '.join(methods)}.",
        )

        # --- The breaker, before anything else. -------------------------------
        if self.breaker.is_open():
            return self._defer(decision, tried, reason="the payment rail is currently unreachable")

        last_error = "no attempt was made"
        for index, method in enumerate(methods):
            # --- Backoff between attempts (never before the first). ----------
            # index 0 is the first attempt and is never delayed; index 1 is the
            # first *retry*, which gets the base backoff.
            delay = self.policy.backoff_for(index - 1) if index > 0 else 0.0
            if delay > 0:
                self.audit.append(
                    ROLE_MPP,
                    Event.RECOVERY_BACKOFF,
                    {"attempt": index + 1, "delay_seconds": delay},
                    f"Waiting {delay:.1f}s before attempt {index + 1}.",
                )
                self._sleep(delay)

            # --- Stock and price, before every attempt. ----------------------
            # Time passes during recovery. Buying something that sold out two
            # retries ago is worse than not buying it at all.
            available, stock_reason = check()
            if not available:
                self.audit.append(
                    ROLE_MPP,
                    Event.STOCK_RECHECK_FAILED,
                    {
                        "attempt": index + 1,
                        "reason": stock_reason,
                        "payment_mandate_id": decision.payment_mandate_id,
                    },
                    f"Stopped before attempt {index + 1}: {stock_reason}. Nothing was charged.",
                )
                outcome = self.processor.finalise_failure(
                    decision,
                    checkout,
                    failure_code=FailureCode.STOCK_UNAVAILABLE,
                    failure_reason=stock_reason,
                    attempts=max(len(tried), 1),
                    method=method,
                )
                return RecoveryResult(
                    outcome=outcome,
                    attempts=len(tried),
                    methods_tried=tuple(tried),
                    human_reason=stock_reason,
                )

            tried.append(method)
            try:
                outcome = self.processor.execute_payment(decision, checkout, method=method)
            except RailDeclined as exc:
                # A definite no. The breaker stays closed; the instrument changes.
                self.breaker.record_decline()
                last_error = exc.message
                self._note_fallback(index, methods, method, exc.message)
                continue
            except (RailTimeout, RailUnavailable) as exc:
                last_error = exc.message
                tripped = self.breaker.record_transport_failure()
                if tripped:
                    self.audit.append(
                        ROLE_MPP,
                        Event.CIRCUIT_OPENED,
                        {
                            "after_attempt": index + 1,
                            "error": exc.code,
                            "breaker": self.breaker.snapshot(),
                        },
                        "The payment rail has failed to answer repeatedly, so the circuit "
                        "breaker is now open. No further attempts will be made until it resets.",
                    )
                    return self._defer(decision, tried, reason=exc.message)
                self._note_fallback(index, methods, method, exc.message)
                continue
            except RailError as exc:  # pragma: no cover — defensive
                last_error = str(exc)
                self._note_fallback(index, methods, method, last_error)
                continue

            # --- Captured. ---------------------------------------------------
            self.breaker.record_success()
            if len(tried) > 1:
                self.audit.append(
                    ROLE_MPP,
                    Event.RECOVERY_SUCCEEDED,
                    {
                        "attempts": len(tried),
                        "methods_tried": tried,
                        "succeeded_on": method,
                        "idempotency_key": outcome.idempotency_key,
                        "payment_id": outcome.receipt.payment_id,
                    },
                    f"Recovered: {method} succeeded on attempt {len(tried)} after "
                    f"{', '.join(tried[:-1])} failed. Same idempotency root throughout, so "
                    "nothing was charged twice.",
                )
            return RecoveryResult(
                outcome=outcome,
                attempts=len(tried),
                methods_tried=tuple(tried),
                human_reason=(
                    f"Paid ₹{paise_to_inr_str(outcome.receipt.amount)} by {method}"
                    + (f" after {len(tried) - 1} failed attempt(s)." if len(tried) > 1 else ".")
                ),
            )

        # --- Exhausted. The stopping rule fired. ------------------------------
        self.audit.append(
            ROLE_MPP,
            Event.RECOVERY_EXHAUSTED,
            {
                "attempts": len(tried),
                "methods_tried": tried,
                "max_attempts": self.policy.max_attempts,
                "last_error": last_error,
                "payment_mandate_id": decision.payment_mandate_id,
            },
            f"All {len(tried)} permitted attempts failed ({', '.join(tried)}). Stopping, as the "
            "playbook requires. Nothing was charged.",
        )
        outcome = self.processor.finalise_failure(
            decision,
            checkout,
            failure_code=FailureCode.EXHAUSTED,
            failure_reason=last_error,
            attempts=len(tried),
            method=tried[-1] if tried else None,
        )
        return RecoveryResult(
            outcome=outcome,
            attempts=len(tried),
            methods_tried=tuple(tried),
            human_reason=f"Payment failed after {len(tried)} attempts: {last_error}",
        )

    # -- helpers ------------------------------------------------------------

    def _note_fallback(self, index: int, methods: list[str], method: str, error: str) -> None:
        remaining = methods[index + 1 :]
        self.audit.append(
            ROLE_MPP,
            Event.RECOVERY_METHOD_FALLBACK,
            {
                "failed_method": method,
                "attempt": index + 1,
                "next_method": remaining[0] if remaining else None,
                "error": error,
            },
            (
                f"{method} failed ({error}); falling back to {remaining[0]}."
                if remaining
                else f"{method} failed ({error}) and there are no instruments left to try."
            ),
        )

    def _defer(self, decision: Decision, tried: list[str], *, reason: str) -> RecoveryResult:
        """Stop without a receipt. The mandate is still good; the rail is not.

        Deliberately issues no terminal receipt. The idempotency record stays
        ``in_flight``, the nonce stays attributed to this mandate, and the agent
        can present the very same mandate on the next tick — where the capture
        probe will first ask the rail whether the deferred attempt actually went
        through.
        """
        human = (
            f"Deferred: {reason}. The mandate has not been spent and can be presented again "
            "once the rail recovers."
        )
        self.audit.append(
            ROLE_MPP,
            Event.CIRCUIT_DEFERRED,
            {
                "payment_mandate_id": decision.payment_mandate_id,
                "amount": decision.amount,
                "attempts": len(tried),
                "breaker": self.breaker.snapshot(),
                "mandate_spent": False,
            },
            human,
        )
        return RecoveryResult(
            outcome=None,
            attempts=len(tried),
            methods_tried=tuple(tried),
            deferred=True,
            human_reason=human,
        )
