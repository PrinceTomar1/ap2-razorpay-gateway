"""The deterministic verifier. Every money decision is made here, in plain code.

    "When this document refers to validation or processing for a particular
    role, it MUST happen in deterministic code regardless of whether the role is
    agentic or not."
        — AP2 v0.2, docs/ap2/specification.md

There is no language model in this module, no network call, and no I/O beyond a
read-only :class:`~gateway.ledger.LedgerView`. Given the same mandates, the same
checkout and the same ledger state it returns the same :class:`Decision` every
time, and it returns it in microseconds.

That is not a limitation we are working around. A verifier is a *classifier over
a small, fully specified domain*: does this signature check out, is this integer
below that integer, is this string in that list. Code does that perfectly and
explains itself. A model does it probabilistically, cannot be audited after the
fact, cannot be reasoned about under adversarial input, and adds a network
dependency to the one path that must never be unavailable. Reaching for one here
would be the single worst engineering decision in this repository. See
ARCHITECTURE.md, "Where we deliberately do NOT use an LLM".

Three outcomes, and the distinction between the last two is the interesting part:

``ALLOW``
    Every check passed. The caller may release funds — once.

``DENY``
    A bound was *violated*. The agent presented a properly-formed closed mandate
    asking for something it was not authorised to do: over the per-transaction
    ceiling, past the daily budget, to a payee that is not on the list, with a
    replayed nonce. There is no path forward for this request, and the answer is
    a reason object rather than an exception.

``UNRESOLVED_CONSTRAINT``
    The agent presented its *standing* authorisation and that authorisation is
    not sufficient on its own — so a constraint is unresolved, in AP2's exact
    sense, and a human must resolve it. This is not an error. It is the protocol
    working: the agent correctly recognised it was at the edge of its authority
    and asked instead of forcing. The caller routes it to the Trusted Surface.

An agent that *knows* it is over its limit should escalate (UNRESOLVED). An agent
that pushes a closed mandate over the limit anyway gets DENY. Both are covered by
tests; the split is what makes the gate a gate rather than a suggestion.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from ap2_min.models import (
    AllowedPayeesConstraint,
    AmountRangeConstraint,
    BudgetConstraint,
    ExecutionDateConstraint,
    PaymentMandateContents,
    ReferenceConstraint,
    paise_to_inr_str,
)
from ap2_min.roles import ROLE_SHOPPING_AGENT, ROLE_TRUSTED_SURFACE, ROLE_USER
from ap2_min.vct import VCT_PAYMENT_CLOSED, VCT_PAYMENT_OPEN
from gateway.ledger import LedgerView
from gateway.mandates import (
    KeyRing,
    MandateError,
    checkout_hash,
    load_payment_mandate,
    utcnow,
)

#: Roles entitled to *present* a closed Payment Mandate. The shopping agent
#: presents them in the normal flow; the user (via the Trusted Surface) presents
#: one directly when an escalation is approved.
PRESENTER_ROLES = frozenset({ROLE_SHOPPING_AGENT, ROLE_USER, ROLE_TRUSTED_SURFACE})


class Outcome(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNRESOLVED_CONSTRAINT = "UNRESOLVED_CONSTRAINT"


class Code:
    """Stable machine codes. The agent branches on these; humans read the prose."""

    # Envelope
    BAD_SIGNATURE = "mandate.bad_signature"
    MALFORMED = "mandate.malformed"
    EXPIRED = "mandate.expired"
    UNKNOWN_KEY = "mandate.unknown_key"
    WRONG_ISSUER = "mandate.wrong_issuer"
    WRONG_VCT = "mandate.wrong_vct"
    NOT_KEY_BOUND = "mandate.not_key_bound"
    OPEN_MANDATE_INVALID = "mandate.open_mandate_invalid"

    # Constraints
    PAYEE_NOT_ALLOWED = "payment.allowed_payees.violated"
    AMOUNT_OUT_OF_RANGE = "payment.amount_range.violated"
    BUDGET_EXCEEDED = "payment.budget.exceeded"
    EXECUTION_WINDOW = "payment.execution_date.violated"
    REFERENCE_MISMATCH = "payment.reference.mismatch"
    CURRENCY_MISMATCH = "payment.currency.mismatch"
    REPLAYED_NONCE = "payment.nonce.replayed"

    # Escalation
    NEEDS_CLOSED_MANDATE = "payment.requires_closed_mandate"
    ABOVE_STANDING_LIMIT = "payment.amount_range.above_standing_limit"
    PAYEE_OUTSIDE_STANDING_SCOPE = "payment.allowed_payees.outside_standing_scope"

    OK = "ok"


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check, with the facts it decided on.

    ``detail`` is machine-readable on purpose: an audit row that says
    "budget exceeded" is far less useful six months later than one that says
    ``{"already_spent": 469600, "requested": 129900, "budget": 500000}``.
    """

    name: str
    passed: bool
    code: str = Code.OK
    human_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "passed": self.passed,
            "code": self.code,
            "human_reason": self.human_reason,
            "detail": self.detail,
        }


def _ok(name: str, **detail: Any) -> CheckResult:
    return CheckResult(name=name, passed=True, detail=detail)


def _fail(name: str, code: str, human_reason: str, **detail: Any) -> CheckResult:
    return CheckResult(name=name, passed=False, code=code, human_reason=human_reason, detail=detail)


@dataclass(frozen=True)
class Decision:
    """What the verifier concluded, and every step it took to get there.

    The ``checks`` tuple is written to the audit log row by row. A judge, an
    auditor or a support engineer can read exactly which comparisons ran, on what
    numbers, in what order — which is the whole point of doing this in code.
    """

    outcome: Outcome
    code: str = Code.OK
    human_reason: str | None = None
    checks: tuple[CheckResult, ...] = ()

    # Facts extracted from the mandate, for the caller that acts on ALLOW.
    payment_mandate_id: str | None = None
    open_mandate_id: str | None = None
    nonce: str | None = None
    amount: int | None = None
    currency: str = "INR"
    payee: str | None = None
    payment_instrument: str | None = None
    bound_checkout_hash: str | None = None
    contents: PaymentMandateContents | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    @property
    def needs_human(self) -> bool:
        return self.outcome is Outcome.UNRESOLVED_CONSTRAINT

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "code": self.code,
            "human_reason": self.human_reason,
            "checks": [c.as_dict() for c in self.checks],
            "payment_mandate_id": self.payment_mandate_id,
            "open_mandate_id": self.open_mandate_id,
            "amount": self.amount,
            "currency": self.currency,
            "payee": self.payee,
        }

    def error_response(self) -> dict[str, Any]:
        """The structured error an AP2 client receives for a non-ALLOW decision.

        ``unresolved_constraint`` is AP2's own error shape and names the specific
        constraint that could not be resolved, so the agent knows what to ask a
        human for rather than just that it failed.
        """
        if self.outcome is Outcome.UNRESOLVED_CONSTRAINT:
            return {
                "error": "unresolved_constraint",
                "constraint": self.code,
                "human_reason": self.human_reason,
                "amount": self.amount,
                "currency": self.currency,
                "checks": [c.as_dict() for c in self.checks if not c.passed],
            }
        return {
            "error": "denied",
            "code": self.code,
            "human_reason": self.human_reason,
            "checks": [c.as_dict() for c in self.checks if not c.passed],
        }


# ---------------------------------------------------------------------------
# The individual checks.
#
# Each one is a small pure function: inputs in, CheckResult out, no I/O except
# the read-only LedgerView where the AP2 evaluation algorithm requires state.
# They are separately testable, separately auditable, and separately readable —
# which is what "explainable" has to mean for a money decision.
# ---------------------------------------------------------------------------


def check_vct(contents: PaymentMandateContents, *, required: str) -> CheckResult:
    """The ``vct`` claim must equal ``required``, byte for byte.

    AP2: "Implementations MUST match the exact ``vct`` string, including the
    version suffix." No prefix matching, no version-range tolerance. A mandate
    claiming ``mandate.payment.2`` is a mandate this code has never been reviewed
    against, and treating it as close enough is how a protocol upgrade becomes a
    vulnerability.
    """
    if contents.vct == required:
        return _ok("vct", vct=contents.vct)
    return _fail(
        "vct",
        Code.WRONG_VCT,
        f"This mandate declares type {contents.vct!r}; this step requires exactly {required!r}.",
        expected=required,
        actual=contents.vct,
    )


def check_presenter_role(presenter_role: str) -> CheckResult:
    """Only a shopping agent or the user may present a closed Payment Mandate.

    A merchant that could mint the mandate authorising its own payment is not a
    payments system.
    """
    if presenter_role in PRESENTER_ROLES:
        return _ok("presenter_role", role=presenter_role)
    return _fail(
        "presenter_role",
        Code.WRONG_ISSUER,
        f"A {presenter_role.replace('_', ' ')} may not present a payment mandate.",
        role=presenter_role,
        allowed=sorted(PRESENTER_ROLES),
    )


def check_not_expired(
    claims: dict[str, Any], *, now: datetime, skew_seconds: int, label: str
) -> CheckResult:
    """``exp`` must be in the future and ``iat`` must not be in the future.

    The JWS layer already enforces ``exp``; this repeats it explicitly because
    the verifier's job is to be readable as a list of the conditions under which
    money moves, and "the mandate has not expired" is one of those conditions. A
    reader should not have to know that PyJWT was configured correctly.

    ``skew_seconds`` tolerates ordinary clock drift between the agent's machine
    and ours. It is a bound, not a blank cheque.
    """
    now_ts = now.timestamp()
    exp = float(claims.get("exp", 0))
    iat = float(claims.get("iat", 0))
    if exp + skew_seconds < now_ts:
        age = int(now_ts - exp)
        return _fail(
            "not_expired",
            Code.EXPIRED,
            f"The {label} expired {age}s ago.",
            which=label,
            exp=exp,
            now=now_ts,
        )
    if iat - skew_seconds > now_ts:
        return _fail(
            "not_expired",
            Code.EXPIRED,
            f"The {label} claims to have been issued in the future.",
            which=label,
            iat=iat,
            now=now_ts,
        )
    return _ok("not_expired", which=label, exp=exp, seconds_remaining=int(exp - now_ts))


def check_key_binding(
    open_mandate: PaymentMandateContents, presenter_kid: str, keyring: KeyRing
) -> CheckResult:
    """The key presenting the closed mandate must be the key the user delegated to.

    The user's open Payment Mandate carries a ``cnf`` claim (RFC 7800) naming the
    public key entitled to present transactions under it. Without this check, an
    open mandate that leaked — a log line, a compromised agent host, a shared
    cache — could be replayed by anybody, and every downstream constraint would
    still pass, because they all describe *what* may be bought, not *who* may buy
    it.

    A mandate with no ``cnf`` is treated as unbound and rejected, rather than
    quietly waved through. Failing open on a missing security claim is how these
    checks stop meaning anything.
    """
    cnf = open_mandate.cnf or {}
    bound_jwk = cnf.get("jwk") if isinstance(cnf, dict) else None
    if not bound_jwk:
        return _fail(
            "key_binding",
            Code.NOT_KEY_BOUND,
            "The standing authorisation names no holder key, so no agent can prove it holds it.",
            open_mandate_id=open_mandate.mandate_id,
        )
    try:
        trusted = keyring.get(presenter_kid)
    except MandateError as exc:
        return _fail("key_binding", Code.UNKNOWN_KEY, exc.message, presenter_kid=presenter_kid)
    # Compare the public coordinates, not the PEM text: the same key can be
    # serialised more than one way.
    public = load_pem_public_key(trusted.public_pem.encode("ascii"))
    numbers = getattr(public, "public_numbers", None)
    if numbers is None:  # pragma: no cover — non-EC key in the ring
        return _fail(
            "key_binding",
            Code.NOT_KEY_BOUND,
            "The presenting key is not an EC key.",
            presenter_kid=presenter_kid,
        )
    presenter_jwk = _jwk_from_public_numbers(numbers())
    if presenter_jwk != bound_jwk:
        return _fail(
            "key_binding",
            Code.NOT_KEY_BOUND,
            "This agent is not the one the standing authorisation was issued to.",
            presenter_kid=presenter_kid,
            open_mandate_id=open_mandate.mandate_id,
        )
    return _ok("key_binding", presenter_kid=presenter_kid)


def _jwk_from_public_numbers(numbers: Any) -> dict[str, str]:
    def b64u(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode("ascii")

    return {"kty": "EC", "crv": "P-256", "x": b64u(numbers.x), "y": b64u(numbers.y)}


def check_currency(
    closed: PaymentMandateContents, open_mandate: PaymentMandateContents
) -> CheckResult:
    """The transaction currency must match the authorisation's.

    Mixing currencies would make every numeric comparison below meaningless:
    ``1500 < 5000`` is true whether or not those are the same unit.
    """
    if closed.currency != open_mandate.currency:
        return _fail(
            "currency",
            Code.CURRENCY_MISMATCH,
            f"The transaction is in {closed.currency} but the authorisation is in "
            f"{open_mandate.currency}.",
            transaction=closed.currency,
            authorisation=open_mandate.currency,
        )
    return _ok("currency", currency=closed.currency)


def check_payee_allowed(
    closed: PaymentMandateContents, open_mandate: PaymentMandateContents
) -> CheckResult:
    """``payment.allowed_payees``: the payee must be on the user's list.

    Spec: "The ``payee`` property of the Payment Mandate MUST be present in the
    ``allowed`` array."

    A mandate with no such constraint has not restricted the payee, so this
    passes — absence of a constraint is not a constraint that fails.
    """
    constraint = open_mandate.constraint("payment.allowed_payees")
    if constraint is None:
        return _ok("allowed_payees", constrained=False)
    assert isinstance(constraint, AllowedPayeesConstraint)
    payee = closed.payee or ""
    if payee in constraint.allowed:
        return _ok("allowed_payees", payee=payee, allowed=constraint.allowed)
    return _fail(
        "allowed_payees",
        Code.PAYEE_NOT_ALLOWED,
        f"{payee} is not one of the merchants this authorisation covers.",
        payee=payee,
        allowed=constraint.allowed,
    )


def check_amount_in_range(
    closed: PaymentMandateContents, open_mandate: PaymentMandateContents
) -> CheckResult:
    """``payment.amount_range``: the per-transaction floor and ceiling.

    Spec: "The ``payment_amount`` property of the Payment Mandate MUST be within
    the range defined by ``min`` and ``max``. The ``currency`` property of the
    Payment Mandate MUST match the ``currency`` property of this constraint."

    Integer paise, inclusive bounds. No rounding, no tolerance, no float.
    """
    constraint = open_mandate.constraint("payment.amount_range")
    if constraint is None:
        return _ok("amount_range", constrained=False)
    assert isinstance(constraint, AmountRangeConstraint)
    amount = closed.payment_amount or 0
    if closed.currency != constraint.currency:
        return _fail(
            "amount_range",
            Code.CURRENCY_MISMATCH,
            f"The amount is in {closed.currency} but the limit is set in {constraint.currency}.",
            transaction_currency=closed.currency,
            constraint_currency=constraint.currency,
        )
    if amount < constraint.min:
        return _fail(
            "amount_range",
            Code.AMOUNT_OUT_OF_RANGE,
            f"₹{paise_to_inr_str(amount)} is below the ₹{paise_to_inr_str(constraint.min)} "
            "minimum for this authorisation.",
            amount=amount,
            min=constraint.min,
            max=constraint.max,
        )
    if amount > constraint.max:
        return _fail(
            "amount_range",
            Code.AMOUNT_OUT_OF_RANGE,
            f"₹{paise_to_inr_str(amount)} is above the ₹{paise_to_inr_str(constraint.max)} "
            "per-purchase limit on this authorisation.",
            amount=amount,
            min=constraint.min,
            max=constraint.max,
            over_by=amount - constraint.max,
        )
    return _ok("amount_range", amount=amount, min=constraint.min, max=constraint.max)


def check_budget(
    closed: PaymentMandateContents,
    open_mandate: PaymentMandateContents,
    ledger: LedgerView,
) -> CheckResult:
    """``payment.budget``: the cumulative ceiling across every transaction.

    Spec: "the requested amount plus the total sum of amounts from previously
    closed Payment Mandates MUST be less than or equal to ``max``. After
    approval, the amount MUST be added to the accumulated total for future
    evaluation."

    This is the one constraint that cannot be decided from the mandate alone —
    it needs the accumulated total, which is why a :class:`LedgerView` is a
    parameter. The view is deliberately read-only: adding to the total is the
    payment processor's job, and only after a capture. A declined payment must
    not consume budget, or a hostile counterparty could exhaust a user's daily
    limit by declining every attempt.
    """
    constraint = open_mandate.constraint("payment.budget")
    if constraint is None:
        return _ok("budget", constrained=False)
    assert isinstance(constraint, BudgetConstraint)
    amount = closed.payment_amount or 0
    already = ledger.spent_under(open_mandate.mandate_id)
    projected = already + amount
    if projected > constraint.max:
        return _fail(
            "budget",
            Code.BUDGET_EXCEEDED,
            f"₹{paise_to_inr_str(amount)} would take today's spend to "
            f"₹{paise_to_inr_str(projected)}, past the ₹{paise_to_inr_str(constraint.max)} budget "
            f"(₹{paise_to_inr_str(max(0, constraint.max - already))} left).",
            already_spent=already,
            requested=amount,
            projected=projected,
            budget=constraint.max,
            over_by=projected - constraint.max,
        )
    return _ok(
        "budget",
        already_spent=already,
        requested=amount,
        projected=projected,
        budget=constraint.max,
        remaining_after=constraint.max - projected,
    )


def check_execution_date(
    closed: PaymentMandateContents,
    open_mandate: PaymentMandateContents,
    *,
    now: datetime,
    skew_seconds: int,
) -> CheckResult:
    """``payment.execution_date``: the window the payment may run in.

    Spec: "The ``execution_date`` of the Payment Mandate MUST be later than or
    equal to ``not_before`` (if present) and earlier than or equal to
    ``not_after`` (if present)."

    We additionally require the stated ``execution_date`` to be near *now*. A
    mandate whose execution date is next Tuesday must not be executable today
    just because next Tuesday is inside the window — the spec constrains the
    declared date, and we constrain the declaration to be honest.
    """
    execution = closed.execution_date
    if execution is None:
        return _fail(
            "execution_date",
            Code.MALFORMED,
            "The mandate does not say when it is meant to execute.",
        )
    drift = abs((execution - now).total_seconds())
    if drift > max(skew_seconds, 300):
        return _fail(
            "execution_date",
            Code.EXECUTION_WINDOW,
            "The mandate's execution date is not now.",
            execution_date=execution.isoformat(),
            now=now.isoformat(),
            drift_seconds=int(drift),
        )
    constraint = open_mandate.constraint("payment.execution_date")
    if constraint is None:
        return _ok("execution_date", execution_date=execution.isoformat(), constrained=False)
    assert isinstance(constraint, ExecutionDateConstraint)
    if constraint.not_before is not None and execution < constraint.not_before:
        return _fail(
            "execution_date",
            Code.EXECUTION_WINDOW,
            f"This authorisation does not begin until {constraint.not_before.isoformat()}.",
            execution_date=execution.isoformat(),
            not_before=constraint.not_before.isoformat(),
        )
    if constraint.not_after is not None and execution > constraint.not_after:
        return _fail(
            "execution_date",
            Code.EXECUTION_WINDOW,
            f"This authorisation lapsed on {constraint.not_after.isoformat()}.",
            execution_date=execution.isoformat(),
            not_after=constraint.not_after.isoformat(),
        )
    return _ok("execution_date", execution_date=execution.isoformat(), constrained=True)


def check_checkout_reference(
    closed: PaymentMandateContents,
    open_mandate: PaymentMandateContents,
    checkout_jwt: str,
) -> CheckResult:
    """The mandate must be bound to *this* checkout, and only this one.

    Two bindings are enforced:

    1. The closed mandate's ``checkout_hash`` must equal sha-256 of the merchant's
       signed Checkout Mandate we are being asked to pay for. Without this, an
       agent could obtain authorisation for a ₹200 cart and present it against a
       ₹200 *different* cart — same amount, same payee, different goods.
    2. If the open mandate carries a ``payment.reference`` constraint, its
       ``conditional_transaction_id`` must match too. Spec: "The Checkout Mandate
       for the approved order MUST contain an open Checkout Mandate with a
       matching hash in its delegate chain."
    """
    expected = checkout_hash(checkout_jwt)
    if closed.checkout_hash != expected:
        return _fail(
            "checkout_reference",
            Code.REFERENCE_MISMATCH,
            "This payment mandate authorises a different checkout.",
            expected=expected,
            presented=closed.checkout_hash,
        )
    constraint = open_mandate.constraint("payment.reference")
    if constraint is not None:
        assert isinstance(constraint, ReferenceConstraint)
        if constraint.conditional_transaction_id != expected:
            return _fail(
                "checkout_reference",
                Code.REFERENCE_MISMATCH,
                "The standing authorisation is pinned to a different checkout.",
                expected=expected,
                pinned_to=constraint.conditional_transaction_id,
            )
    return _ok("checkout_reference", checkout_hash=expected, pinned=constraint is not None)


def check_nonce_unused(closed: PaymentMandateContents, ledger: LedgerView) -> CheckResult:
    """Replay: a mandate's single-use nonce must not have been accepted before.

    The nonce is burned when a mandate is accepted, and attributed to the mandate
    that burned it. A *different* mandate presenting a burned nonce is a replay
    and is refused. The *same* mandate presenting it again is not: that is a
    retry, and refusing it would break recovery — when the circuit breaker defers
    a payment, the mandate must remain presentable on the next tick.

    Idempotency and replay detection answer different questions. Idempotency asks
    "have I already answered this exact request?" and is settled by the store in
    gateway/payments.py. Replay detection asks "is someone reusing a token that
    was only good once?" and is settled here.
    """
    owner = ledger.nonce_owner(closed.nonce)
    if owner is not None and owner != closed.mandate_id:
        return _fail(
            "nonce",
            Code.REPLAYED_NONCE,
            "This authorisation token was already used by a different mandate.",
            nonce=closed.nonce,
            payment_mandate_id=closed.mandate_id,
            burned_by=owner,
        )
    return _ok("nonce", nonce=closed.nonce, first_use=owner is None)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def verify_payment_mandate(
    payment_mandate_jwt: str,
    checkout_jwt: str,
    ledger: LedgerView,
    *,
    keyring: KeyRing,
    now: datetime | None = None,
    clock_skew_seconds: int = 30,
    checkout_total: int | None = None,
) -> Decision:
    """Decide whether this payment may proceed. The only money decision there is.

    ``checkout_total`` is optional and used only to explain an escalation: when an
    agent presents an *open* mandate because it knows the cart is over its limit,
    we want the resulting ``unresolved_constraint`` to name the actual gap rather
    than just say "insufficient authority".

    Returns a :class:`Decision`. Never raises for a bad mandate — a malformed or
    forged mandate is an answer (``DENY``), not an exception, and callers on the
    money path should not be writing ``try`` blocks around a policy question.
    """
    moment = now or utcnow()
    checks: list[CheckResult] = []

    # ---- 1. Envelope. Signature, structure, trusted key. ------------------
    # Nothing below this point runs on an unverified mandate, and nothing at all
    # reaches Razorpay if this fails: failure mode 3.
    try:
        closed, claims = load_payment_mandate(payment_mandate_jwt, keyring)
    except MandateError as exc:
        return Decision(
            outcome=Outcome.DENY,
            code=exc.code,
            human_reason=f"The payment mandate was rejected at the boundary: {exc.message}.",
            checks=(_fail("signature", exc.code, exc.message, error_detail=exc.detail),),
        )
    presenter_kid = str(claims["iss"])
    presenter_role = keyring.get(presenter_kid).role
    checks.append(_ok("signature", kid=presenter_kid, role=presenter_role, alg="ES256"))

    # ---- 2. Is this even a closed mandate? --------------------------------
    # An OPEN mandate presented here is the agent saying "this is all the
    # authority I have, and I don't think it's enough". That is an unresolved
    # constraint, not a violation.
    if closed.vct == VCT_PAYMENT_OPEN:
        return _escalate(closed, checks, checkout_total=checkout_total, ledger=ledger)

    vct_check = check_vct(closed, required=VCT_PAYMENT_CLOSED)
    checks.append(vct_check)
    if not vct_check.passed:
        return _deny(vct_check, checks, closed)

    role_check = check_presenter_role(presenter_role)
    checks.append(role_check)
    if not role_check.passed:
        return _deny(role_check, checks, closed)

    expiry_check = check_not_expired(
        claims, now=moment, skew_seconds=clock_skew_seconds, label="payment mandate"
    )
    checks.append(expiry_check)
    if not expiry_check.passed:
        return _deny(expiry_check, checks, closed)

    # ---- 3. The standing authorisation it claims to draw from -------------
    # Verified independently, and required to be user-signed. The agent embedded
    # it, but the agent's word for its own authority is worth nothing.
    assert closed.open_mandate_jws is not None  # guaranteed by the model validator
    try:
        open_mandate, open_claims = load_payment_mandate(
            closed.open_mandate_jws, keyring, expected_role=ROLE_USER
        )
    except MandateError as exc:
        # One code for "your standing authorisation is the problem", with the
        # specific cause underneath. The agent needs the distinction: a bad open
        # mandate means "go ask the user for a new one", a bad closed mandate
        # means "re-sign and retry".
        result = _fail(
            "open_mandate",
            Code.OPEN_MANDATE_INVALID,
            f"The standing authorisation behind this payment is not valid: {exc.message}.",
            underlying_code=exc.code,
            error_detail=exc.detail,
        )
        checks.append(result)
        return _deny(result, checks, closed)

    open_vct_check = check_vct(open_mandate, required=VCT_PAYMENT_OPEN)
    checks.append(open_vct_check)
    if not open_vct_check.passed:
        return _deny(open_vct_check, checks, closed)

    open_expiry_check = check_not_expired(
        open_claims, now=moment, skew_seconds=clock_skew_seconds, label="standing authorisation"
    )
    checks.append(open_expiry_check)
    if not open_expiry_check.passed:
        return _deny(open_expiry_check, checks, closed, open_mandate)

    # ---- 4. Every remaining check, in order, short-circuiting on the first
    #         failure so the reason we report is the reason it stopped. -----
    remaining: Sequence[CheckResult] = (
        check_key_binding(open_mandate, presenter_kid, keyring),
        check_currency(closed, open_mandate),
        check_payee_allowed(closed, open_mandate),
        check_amount_in_range(closed, open_mandate),
        check_budget(closed, open_mandate, ledger),
        check_execution_date(closed, open_mandate, now=moment, skew_seconds=clock_skew_seconds),
        check_checkout_reference(closed, open_mandate, checkout_jwt),
        check_nonce_unused(closed, ledger),
    )
    for result in remaining:
        checks.append(result)
        if not result.passed:
            return _deny(result, checks, closed, open_mandate)

    return Decision(
        outcome=Outcome.ALLOW,
        code=Code.OK,
        human_reason=(
            f"Approved ₹{paise_to_inr_str(closed.payment_amount or 0)} to "
            f"{closed.payee_name or closed.payee}: signed by the delegated agent key, within the "
            f"per-purchase limit and the remaining daily budget, bound to this checkout, "
            f"first use of this token."
        ),
        checks=tuple(checks),
        payment_mandate_id=closed.mandate_id,
        open_mandate_id=open_mandate.mandate_id,
        nonce=closed.nonce,
        amount=closed.payment_amount,
        currency=closed.currency,
        payee=closed.payee,
        payment_instrument=closed.payment_instrument,
        bound_checkout_hash=closed.checkout_hash,
        contents=closed,
    )


def _deny(
    failure: CheckResult,
    checks: list[CheckResult],
    closed: PaymentMandateContents,
    open_mandate: PaymentMandateContents | None = None,
) -> Decision:
    return Decision(
        outcome=Outcome.DENY,
        code=failure.code,
        human_reason=failure.human_reason,
        checks=tuple(checks),
        payment_mandate_id=closed.mandate_id,
        open_mandate_id=open_mandate.mandate_id if open_mandate else None,
        nonce=closed.nonce,
        amount=closed.payment_amount,
        currency=closed.currency,
        payee=closed.payee,
        payment_instrument=closed.payment_instrument,
        bound_checkout_hash=closed.checkout_hash,
        contents=closed,
    )


def _escalate(
    open_mandate: PaymentMandateContents,
    checks: list[CheckResult],
    *,
    checkout_total: int | None,
    ledger: LedgerView,
) -> Decision:
    """Build the ``unresolved_constraint`` for an open mandate presented for payment.

    We name the constraint that is actually unresolved, so the Trusted Surface can
    show the user a specific question ("₹4,999 is above your ₹1,500 limit —
    approve this one?") rather than a generic consent prompt. A gate a user
    cannot understand is a gate a user will click through.
    """
    amount = checkout_total
    code = Code.NEEDS_CLOSED_MANDATE
    reason = (
        "The agent holds a standing authorisation but no per-transaction mandate for this "
        "purchase, so it needs your approval."
    )
    detail: dict[str, Any] = {"open_mandate_id": open_mandate.mandate_id}

    if amount is not None:
        range_constraint = open_mandate.constraint("payment.amount_range")
        budget_constraint = open_mandate.constraint("payment.budget")
        if isinstance(range_constraint, AmountRangeConstraint) and amount > range_constraint.max:
            code = Code.ABOVE_STANDING_LIMIT
            reason = (
                f"₹{paise_to_inr_str(amount)} is above the ₹{paise_to_inr_str(range_constraint.max)}"
                " per-purchase limit on this standing authorisation, so it needs your approval."
            )
            detail |= {"amount": amount, "limit": range_constraint.max}
        elif isinstance(budget_constraint, BudgetConstraint):
            already = ledger.spent_under(open_mandate.mandate_id)
            if already + amount > budget_constraint.max:
                code = Code.ABOVE_STANDING_LIMIT
                reason = (
                    f"₹{paise_to_inr_str(amount)} would take today's spend past the "
                    f"₹{paise_to_inr_str(budget_constraint.max)} budget, so it needs your approval."
                )
                detail |= {
                    "amount": amount,
                    "already_spent": already,
                    "budget": budget_constraint.max,
                }

    checks.append(
        CheckResult(
            name="standing_authorisation_sufficient",
            passed=False,
            code=code,
            human_reason=reason,
            detail=detail,
        )
    )
    return Decision(
        outcome=Outcome.UNRESOLVED_CONSTRAINT,
        code=code,
        human_reason=reason,
        checks=tuple(checks),
        payment_mandate_id=open_mandate.mandate_id,
        open_mandate_id=open_mandate.mandate_id,
        nonce=open_mandate.nonce,
        amount=amount,
        currency=open_mandate.currency,
        contents=open_mandate,
    )
