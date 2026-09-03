"""Constructors for well-formed mandates.

These build the *contents*; they never sign. Signing is an authority question and
belongs to whoever holds the key — the user's Trusted Surface, the merchant, the
shopping agent. Keeping construction and signing apart means a test, the agent
and the gateway all build mandates the same way and only differ in who signs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ap2_min.models import (
    AllowedMerchantsConstraint,
    AllowedPayeesConstraint,
    AmountRangeConstraint,
    BudgetConstraint,
    Cart,
    CheckoutAmountCeilingConstraint,
    CheckoutConstraint,
    CheckoutMandateContents,
    CheckoutShipToConstraint,
    ExecutionDateConstraint,
    PaymentConstraint,
    PaymentMandateContents,
    ReferenceConstraint,
)
from ap2_min.vct import (
    VCT_CHECKOUT_CLOSED,
    VCT_CHECKOUT_OPEN,
    VCT_PAYMENT_CLOSED,
    VCT_PAYMENT_OPEN,
)


def _new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _new_nonce() -> str:
    import uuid

    return uuid.uuid4().hex


def open_checkout_mandate(
    *,
    allowed_merchants: list[str],
    max_amount: int,
    ship_to_pincode: str,
    cnf: dict[str, Any] | None = None,
    mandate_id: str | None = None,
) -> CheckoutMandateContents:
    """The user's standing checkout authorisation: where, how much, ship to where.

    Built as a typed ``constraints`` array per docs/ap2/checkout_mandate.md —
    ``checkout.allowed_merchants`` is the spec's own; the ceiling and the
    delivery pincode are documented extensions (see the models module).
    """
    constraints: list[CheckoutConstraint] = [
        AllowedMerchantsConstraint(allowed=list(allowed_merchants)),
        CheckoutAmountCeilingConstraint(max=max_amount),
        CheckoutShipToConstraint(pincode=ship_to_pincode),
    ]
    return CheckoutMandateContents(
        vct=VCT_CHECKOUT_OPEN,
        mandate_id=mandate_id or _new_id("cmo"),
        constraints=constraints,
        cnf=cnf,
    )


def closed_checkout_mandate(
    *,
    cart: Cart,
    delegate_chain: list[str] | None = None,
    mandate_id: str | None = None,
) -> CheckoutMandateContents:
    """The merchant's signed guarantee of one cart at one price.

    ``delegate_chain`` carries the hash of the open Checkout Mandate this cart was
    assembled under, which is what a ``payment.reference`` constraint resolves
    against.
    """
    return CheckoutMandateContents(
        vct=VCT_CHECKOUT_CLOSED,
        mandate_id=mandate_id or _new_id("cm"),
        cart=cart,
        delegate_chain=delegate_chain or [],
    )


def open_payment_mandate(
    *,
    budget: int | None = None,
    amount_min: int = 0,
    amount_max: int | None = None,
    allowed_payees: list[str] | None = None,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    pinned_checkout_hash: str | None = None,
    cnf: dict[str, Any] | None = None,
    mandate_id: str | None = None,
    nonce: str | None = None,
) -> PaymentMandateContents:
    """The user's standing payment authorisation, as a list of AP2 constraints.

    Every argument is optional because AP2 constraints are additive: an
    authorisation that omits ``payment.budget`` has not set a cumulative ceiling,
    and the verifier says so explicitly rather than inventing a default. In
    practice the Trusted Surface always sets all of them — an unbounded standing
    authorisation is exactly the liability this project exists to remove.
    """
    constraints: list[PaymentConstraint] = []
    if amount_max is not None:
        constraints.append(AmountRangeConstraint(min=amount_min, max=amount_max))
    if budget is not None:
        constraints.append(BudgetConstraint(max=budget))
    if allowed_payees:
        constraints.append(AllowedPayeesConstraint(allowed=allowed_payees))
    if not_before is not None or not_after is not None:
        constraints.append(ExecutionDateConstraint(not_before=not_before, not_after=not_after))
    if pinned_checkout_hash is not None:
        constraints.append(ReferenceConstraint(conditional_transaction_id=pinned_checkout_hash))
    if not constraints:
        raise ValueError("an open Payment Mandate with no constraints authorises everything")
    return PaymentMandateContents(
        vct=VCT_PAYMENT_OPEN,
        mandate_id=mandate_id or _new_id("pmo"),
        nonce=nonce or _new_nonce(),
        constraints=constraints,
        cnf=cnf,
    )


def closed_payment_mandate(
    *,
    payee: str,
    payee_name: str,
    amount: int,
    payment_instrument: str,
    checkout_hash: str,
    open_mandate_jws: str,
    execution_date: datetime,
    transaction_id: str | None = None,
    mandate_id: str | None = None,
    nonce: str | None = None,
) -> PaymentMandateContents:
    """One transaction: this much, to this payee, for this checkout, now."""
    return PaymentMandateContents(
        vct=VCT_PAYMENT_CLOSED,
        mandate_id=mandate_id or _new_id("pm"),
        nonce=nonce or _new_nonce(),
        transaction_id=transaction_id or _new_id("txn"),
        payee=payee,
        payee_name=payee_name,
        payment_amount=amount,
        payment_instrument=payment_instrument,
        checkout_hash=checkout_hash,
        execution_date=execution_date,
        open_mandate_jws=open_mandate_jws,
    )


def validity_window(hours: int, *, now: datetime) -> tuple[datetime, datetime]:
    """A ``payment.execution_date`` window starting now."""
    return now, now + timedelta(hours=hours)
