"""The payment rail: one protocol, two implementations.

:class:`PaymentRail` is the whole surface the Merchant Payment Processor needs
from a payment provider — four methods. :class:`RazorpayRail` implements it with
the official Razorpay SDK against **test-mode keys only**; :class:`FakeRail`
implements it in memory, deterministically, with a control surface for
programming declines and timeouts.

Why the seam is here rather than deeper
---------------------------------------
Everything above this line — mandates, the verifier, idempotency, recovery, the
audit chain — is rail-agnostic and runs identically against either
implementation. That means the entire failure-recovery story is testable offline
and reproducibly, which matters because *the interesting behaviour of a payments
system is almost entirely in what happens when the rail misbehaves*, and you
cannot make a real sandbox time out on demand.

``PAYMENT_RAIL=fake`` is the default. Every test and ``make demo`` use FakeRail.
``PAYMENT_RAIL=razorpay`` with test keys is the live check.

Live-mode safety
----------------
:class:`RazorpayRail` refuses to construct with a key id that does not start with
``rzp_test_``. A live key in this repository would be a bug that costs real money,
so it is checked in code and not left to a README warning.
"""

from __future__ import annotations

import dataclasses
import itertools
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

CURRENCY_INR = "INR"

#: Razorpay's documented test VPAs. Paying a test-mode link from these drives a
#: deterministic outcome, which is what makes the live check reproducible.
TEST_VPA_SUCCESS = "success@razorpay"
TEST_VPA_FAILURE = "failure@razorpay"

#: Instrument names this project uses. They map onto Razorpay methods.
METHOD_UPI = "upi"
METHOD_PAYMENT_LINK = "payment_link"
METHOD_CARD = "card"


# ---------------------------------------------------------------------------
# Errors
#
# The distinction between these three is what the recovery playbook branches on,
# so they are part of the contract rather than incidental exception types:
#
#   RailDeclined    — the rail answered, and the answer was no. Deterministic.
#                     Retrying the same instrument will get the same no.
#   RailTimeout     — the rail did not answer. The request MAY have been
#                     processed. This is the dangerous one, and the only reason
#                     the idempotency store and the pre-attempt capture probe
#                     exist.
#   RailUnavailable — the rail could not be reached at all. Nothing happened.
# ---------------------------------------------------------------------------


class RailError(Exception):
    """Base class for payment-rail failures."""

    code = "rail.error"


class RailDeclined(RailError):
    """The rail processed the request and declined it. A definite answer."""

    code = "rail.declined"

    def __init__(self, message: str, *, order_id: str, payment: Payment | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.order_id = order_id
        self.payment = payment


class RailTimeout(RailError):
    """The rail did not answer in time. The outcome is genuinely unknown."""

    code = "rail.timeout"

    def __init__(self, message: str, *, order_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.order_id = order_id


class RailUnavailable(RailError):
    """The rail could not be reached, or refused the request outright."""

    code = "rail.unavailable"

    def __init__(self, message: str, *, order_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.order_id = order_id


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Order:
    id: str
    amount: int
    currency: str
    receipt: str
    status: str
    notes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Payment:
    id: str
    order_id: str
    amount: int
    currency: str
    status: str
    method: str
    error_code: str | None = None
    error_description: str | None = None

    @property
    def is_captured(self) -> bool:
        return self.status == "captured"

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "amount": self.amount,
            "currency": self.currency,
            "status": self.status,
            "method": self.method,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class PaymentLinkRef:
    id: str
    short_url: str
    status: str
    amount: int
    reference_id: str


@runtime_checkable
class PaymentRail(Protocol):
    """The only thing the Merchant Payment Processor knows about payments."""

    name: str

    def create_order(
        self, *, amount: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> Order:
        """Create an order for ``amount`` paise. Idempotency is the caller's job."""
        ...

    def complete_test_payment(
        self, order_id: str, *, method: str, vpa: str | None = None
    ) -> Payment:
        """Drive a test-mode payment on ``order_id`` to a terminal state.

        Raises :class:`RailDeclined` on a decline, :class:`RailTimeout` when the
        outcome is unknown, :class:`RailUnavailable` when the rail is unreachable.
        """
        ...

    def fetch_order_payments(self, order_id: str) -> list[Payment]:
        """Every payment attached to an order. The authority on what really happened."""
        ...

    def create_upi_payment_link(
        self, *, amount: int, description: str, reference_id: str
    ) -> PaymentLinkRef:
        """A hosted UPI payment link — the fallback instrument."""
        ...


# ---------------------------------------------------------------------------
# FakeRail
# ---------------------------------------------------------------------------


@dataclass
class _Rule:
    """A programmed misbehaviour, scoped to a reference and optionally a method."""

    kind: str  # decline | timeout | unavailable
    reference: str | None
    methods: frozenset[str] | None
    remaining: int | None

    def matches(self, reference: str, method: str) -> bool:
        if self.remaining is not None and self.remaining <= 0:
            return False
        if self.reference is not None and self.reference != reference:
            return False
        return not (self.methods is not None and method not in self.methods)


class FakeRail:
    """A deterministic in-memory payment rail.

    Same interface as the real one, no network, and — crucially — programmable.
    Real sandboxes cannot be told "time out on the next UPI attempt for this
    mandate, then succeed on the fallback", and that is precisely the scenario
    the recovery playbook exists for.

    Ids are sequential (``order_fake_000001``) so a demo run is byte-for-byte
    reproducible and a diff between two runs is meaningful.
    """

    name = "fake"

    def __init__(self, *, id_prefix: str = "fake") -> None:
        self._prefix = id_prefix
        self._order_seq: Iterator[int] = itertools.count(1)
        self._payment_seq: Iterator[int] = itertools.count(1)
        self._link_seq: Iterator[int] = itertools.count(1)
        self._orders: dict[str, Order] = {}
        self._payments: dict[str, list[Payment]] = {}
        self._links: dict[str, PaymentLinkRef] = {}
        self._rules: list[_Rule] = []
        #: Every call, in order. Tests assert on this to prove that a rejected
        #: mandate never reached the rail at all.
        self.calls: list[tuple[str, str]] = []

    # -- control surface ----------------------------------------------------

    def decline(
        self,
        *,
        reference: str | None = None,
        methods: set[str] | None = None,
        times: int | None = 1,
    ) -> None:
        """Programme a decline: the rail answers, and the answer is no."""
        self._rules.append(
            _Rule("decline", reference, frozenset(methods) if methods else None, times)
        )

    def timeout(
        self,
        *,
        reference: str | None = None,
        methods: set[str] | None = None,
        times: int | None = 1,
    ) -> None:
        """Programme a timeout: the rail does not answer, outcome unknown."""
        self._rules.append(
            _Rule("timeout", reference, frozenset(methods) if methods else None, times)
        )

    def unavailable(
        self,
        *,
        reference: str | None = None,
        methods: set[str] | None = None,
        times: int | None = 1,
    ) -> None:
        """Programme an outright transport failure: nothing happened."""
        self._rules.append(
            _Rule("unavailable", reference, frozenset(methods) if methods else None, times)
        )

    def reset_rules(self) -> None:
        self._rules.clear()

    # -- PaymentRail --------------------------------------------------------

    def create_order(
        self, *, amount: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> Order:
        self.calls.append(("create_order", receipt))
        order = Order(
            id=f"order_{self._prefix}_{next(self._order_seq):06d}",
            amount=amount,
            currency=currency,
            receipt=receipt,
            status="created",
            notes=dict(notes),
        )
        self._orders[order.id] = order
        self._payments.setdefault(order.id, [])
        return order

    def complete_test_payment(
        self, order_id: str, *, method: str, vpa: str | None = None
    ) -> Payment:
        self.calls.append(("complete_test_payment", order_id))
        order = self._orders.get(order_id)
        if order is None:
            raise RailUnavailable(f"unknown order {order_id}", order_id=order_id)

        # A captured order stays captured. Asking the rail to pay an order that
        # is already paid must not create a second payment, in the fake exactly
        # as in the real one.
        for existing in self._payments[order_id]:
            if existing.is_captured:
                return existing

        reference = order.notes.get("reference", order.receipt)
        rule = self._consume_rule(reference, method)
        if rule is not None and rule.kind == "timeout":
            # Deliberately record NOTHING. A timeout means the caller cannot know
            # whether a payment happened, which is what makes the capture probe
            # in gateway/recovery.py necessary rather than decorative.
            raise RailTimeout(
                f"no response from the rail for {order_id} on {method}", order_id=order_id
            )
        if rule is not None and rule.kind == "unavailable":
            raise RailUnavailable(f"rail unreachable for {order_id}", order_id=order_id)

        declined = rule is not None and rule.kind == "decline"
        if method == METHOD_UPI and vpa == TEST_VPA_FAILURE:
            declined = True

        payment = Payment(
            id=f"pay_{self._prefix}_{next(self._payment_seq):06d}",
            order_id=order_id,
            amount=order.amount,
            currency=order.currency,
            status="failed" if declined else "captured",
            method=method,
            error_code="BAD_REQUEST_ERROR" if declined else None,
            error_description="Payment was declined by the bank." if declined else None,
        )
        self._payments[order_id].append(payment)
        if declined:
            raise RailDeclined("the bank declined this payment", order_id=order_id, payment=payment)
        self._orders[order_id] = dataclasses.replace(order, status="paid")
        return payment

    def fetch_order_payments(self, order_id: str) -> list[Payment]:
        self.calls.append(("fetch_order_payments", order_id))
        return list(self._payments.get(order_id, []))

    def create_upi_payment_link(
        self, *, amount: int, description: str, reference_id: str
    ) -> PaymentLinkRef:
        self.calls.append(("create_upi_payment_link", reference_id))
        serial = next(self._link_seq)
        link = PaymentLinkRef(
            id=f"plink_{self._prefix}_{serial:06d}",
            short_url=f"https://rzp.io/i/{self._prefix}{serial:04d}",
            status="created",
            amount=amount,
            reference_id=reference_id,
        )
        self._links[link.id] = link
        return link

    # -- introspection for tests -------------------------------------------

    def orders(self) -> list[Order]:
        return list(self._orders.values())

    def all_payments(self) -> list[Payment]:
        return [p for payments in self._payments.values() for p in payments]

    def captured_total(self) -> int:
        return sum(p.amount for p in self.all_payments() if p.is_captured)

    def _consume_rule(self, reference: str, method: str) -> _Rule | None:
        for rule in self._rules:
            if rule.matches(reference, method):
                if rule.remaining is not None:
                    rule.remaining -= 1
                return rule
        return None


# ---------------------------------------------------------------------------
# RazorpayRail
# ---------------------------------------------------------------------------


class RazorpayRail:
    """The real Razorpay rail, test mode only.

    Implemented from the official API reference (Orders, Payment Links, Payments)
    and the Python SDK's own resource methods. Where the API does not offer what
    this interface wants, we say so rather than inventing a field:

    **There is no server-side API that completes a payment.** A payment is made
    by a human on Razorpay's hosted page. So :meth:`complete_test_payment` does
    the honest thing — it creates a payment link, prints it, and *polls*
    ``order.payments`` until a terminal payment appears or the deadline passes.
    Pay it with ``success@razorpay`` and you get a capture; pay it with
    ``failure@razorpay`` and you get a decline. That is the whole live check, and
    it is documented in docs/RAZORPAY_TESTING.md.

    Everything the gateway does *around* the rail — verification, idempotency,
    recovery, audit — is identical on this path and on FakeRail, which is the
    point of the seam.
    """

    name = "razorpay"

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        poll_timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 3.0,
        notify: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not key_id.startswith("rzp_test_"):
            # Checked in code, not in a README. A live key here spends real money.
            raise ValueError(
                f"refusing to start: RAZORPAY_KEY_ID is {key_id[:12]!r}, which is not a test key. "
                "This project is test-mode only and will never accept an rzp_live_ key."
            )
        if not key_secret:
            raise ValueError("RAZORPAY_KEY_SECRET is empty")

        import razorpay  # imported lazily so the offline path never needs it

        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._client.set_app_details({"title": "ap2-razorpay-gateway", "version": "0.2.0"})
        self.key_id = key_id
        self.poll_timeout_seconds = poll_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._notify = notify or (lambda message: print(message))
        self._sleep = sleep

    # -- PaymentRail --------------------------------------------------------

    def create_order(
        self, *, amount: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> Order:
        """POST /v1/orders.

        ``receipt`` carries our idempotency key so an order can always be traced
        back to the mandate that authorised it, from the Razorpay dashboard alone.
        """
        payload = {
            "amount": amount,  # Razorpay amounts are in paise, same unit we use
            "currency": currency,
            "receipt": receipt[:40],  # Razorpay caps receipt at 40 characters
            "notes": notes,
            "payment_capture": 1,
        }
        raw = self._call(lambda: self._client.order.create(data=payload))
        return Order(
            id=str(raw["id"]),
            amount=int(raw["amount"]),
            currency=str(raw["currency"]),
            receipt=str(raw.get("receipt") or receipt),
            status=str(raw.get("status", "created")),
            notes={k: str(v) for k, v in (raw.get("notes") or {}).items()},
        )

    def complete_test_payment(
        self, order_id: str, *, method: str, vpa: str | None = None
    ) -> Payment:
        """Create a payment link for the order and poll until it resolves.

        See the class docstring: there is no API to pay on a customer's behalf,
        and pretending otherwise would be inventing Razorpay behaviour.
        """
        existing = self.fetch_order_payments(order_id)
        for payment in existing:
            if payment.is_captured:
                return payment

        link = self.create_upi_payment_link(
            amount=self._order_amount(order_id),
            description=f"AP2 gateway test payment ({method})",
            reference_id=order_id,
        )
        self._notify(
            f"\n  Razorpay test mode — pay this link to continue:\n"
            f"    {link.short_url}\n"
            f"    UPI id {TEST_VPA_SUCCESS} to succeed, {TEST_VPA_FAILURE} to decline.\n"
            f"    Waiting up to {int(self.poll_timeout_seconds)}s...\n"
        )

        deadline = time.monotonic() + self.poll_timeout_seconds
        while time.monotonic() < deadline:
            for payment in self.fetch_order_payments(order_id):
                if payment.is_captured:
                    return payment
                if payment.is_failed:
                    raise RailDeclined(
                        payment.error_description or "the bank declined this payment",
                        order_id=order_id,
                        payment=payment,
                    )
            self._sleep(self.poll_interval_seconds)

        # A timeout, not a decline. The recovery playbook must treat these
        # differently: nobody knows whether this payment happened.
        raise RailTimeout(
            f"no terminal payment on {order_id} within {int(self.poll_timeout_seconds)}s",
            order_id=order_id,
        )

    def fetch_order_payments(self, order_id: str) -> list[Payment]:
        """GET /v1/orders/{id}/payments — the source of truth after a timeout."""
        raw = self._call(lambda: self._client.order.payments(order_id))
        return [self._to_payment(item, order_id) for item in raw.get("items", [])]

    def create_upi_payment_link(
        self, *, amount: int, description: str, reference_id: str
    ) -> PaymentLinkRef:
        """POST /v1/payment_links.

        ``options.checkout.method`` restricts the hosted page to UPI. This field
        is documented but is the one thing here we have not been able to verify
        against a live sandbox from this machine — see LIMITATIONS.md. Everything
        else on this path uses core, long-stable fields.
        """
        payload: dict[str, Any] = {
            "amount": amount,
            "currency": CURRENCY_INR,
            "accept_partial": False,
            "description": description[:2048],
            "reference_id": reference_id[:40],
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "options": {"checkout": {"method": {"upi": "1"}}},
        }
        raw = self._call(lambda: self._client.payment_link.create(data=payload))
        return PaymentLinkRef(
            id=str(raw["id"]),
            short_url=str(raw["short_url"]),
            status=str(raw.get("status", "created")),
            amount=int(raw.get("amount", amount)),
            reference_id=str(raw.get("reference_id") or reference_id),
        )

    # -- internals ----------------------------------------------------------

    def _order_amount(self, order_id: str) -> int:
        raw = self._call(lambda: self._client.order.fetch(order_id))
        return int(raw["amount"])

    @staticmethod
    def _to_payment(raw: dict[str, Any], order_id: str) -> Payment:
        return Payment(
            id=str(raw["id"]),
            order_id=str(raw.get("order_id") or order_id),
            amount=int(raw.get("amount", 0)),
            currency=str(raw.get("currency", CURRENCY_INR)),
            status=str(raw.get("status", "created")),
            method=str(raw.get("method") or "unknown"),
            error_code=raw.get("error_code"),
            error_description=raw.get("error_description"),
        )

    def _call(self, fn: Callable[[], Any]) -> dict[str, Any]:
        """Map SDK exceptions onto the three rail errors the playbook branches on.

        A 5xx or a socket error is *not* the same as a declined card, and
        flattening them into one exception type is how a system ends up retrying
        something that already succeeded.
        """
        import requests
        from razorpay import errors as rzp_errors

        try:
            result = fn()
        except rzp_errors.SignatureVerificationError:
            raise
        except rzp_errors.BadRequestError as exc:
            raise RailDeclined(str(exc), order_id="") from exc
        except (rzp_errors.GatewayError, rzp_errors.ServerError) as exc:
            raise RailUnavailable(f"Razorpay returned an error: {exc}") from exc
        except requests.Timeout as exc:
            raise RailTimeout(f"Razorpay request timed out: {exc}") from exc
        except requests.RequestException as exc:
            raise RailUnavailable(f"could not reach Razorpay: {exc}") from exc
        if not isinstance(result, dict):  # pragma: no cover — defensive
            raise RailUnavailable(f"unexpected Razorpay response type: {type(result).__name__}")
        return result


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def build_rail(
    kind: str | None = None, *, notify: Callable[[str], None] | None = None
) -> PaymentRail:
    """Build the rail named by ``$PAYMENT_RAIL``. Defaults to ``fake``.

    Defaulting to the simulator rather than the real thing is deliberate: the
    dangerous default is the one that reaches for the network and a credential
    when a developer forgot to configure one.
    """
    selected = (kind or os.environ.get("PAYMENT_RAIL") or "fake").strip().lower()
    if selected == "fake":
        return FakeRail()
    if selected == "razorpay":
        key_id = os.environ.get("RAZORPAY_KEY_ID", "")
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
        if not key_id or not key_secret:
            raise RuntimeError(
                "PAYMENT_RAIL=razorpay needs RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env. "
                "See docs/RAZORPAY_TESTING.md."
            )
        return RazorpayRail(
            key_id,
            key_secret,
            poll_timeout_seconds=float(os.environ.get("RAZORPAY_POLL_TIMEOUT_SECONDS", "180")),
            poll_interval_seconds=float(os.environ.get("RAZORPAY_POLL_INTERVAL_SECONDS", "3")),
            notify=notify,
        )
    raise ValueError(f"unknown PAYMENT_RAIL {selected!r}; expected 'fake' or 'razorpay'")
