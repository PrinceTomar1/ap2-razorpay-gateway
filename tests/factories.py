"""A small scenario builder so verifier tests read as scenarios, not plumbing.

Every test in test_verify.py is of the shape "given this standing authorisation
and this cart, when the agent presents that, the verifier says X". This module is
the "given".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ap2_min.builders import (
    closed_checkout_mandate,
    closed_payment_mandate,
    open_payment_mandate,
)
from ap2_min.models import Cart, CartItem, PaymentMandateContents, inr
from gateway.mandates import Signer, checkout_hash, utcnow

MERCHANT_ID = "m_stridefit"
MERCHANT_NAME = "StrideFit Sportswear"


def cart(
    total: int = inr(1299),
    *,
    cart_id: str = "cart_test_1",
    merchant_id: str = MERCHANT_ID,
    sku: str = "SF-RUN-001",
    pincode: str = "560001",
) -> Cart:
    return Cart(
        cart_id=cart_id,
        merchant_id=merchant_id,
        merchant_name=MERCHANT_NAME,
        items=[
            CartItem(
                sku=sku, name="Velocity Road Runner", qty=1, unit_price=total, line_total=total
            )
        ],
        total=total,
        ship_to_pincode=pincode,
    )


@dataclass
class Scenario:
    """One user, one agent, one merchant, one cart — fully signed and ready.

    Defaults mirror config/policy.yaml: ₹5,000 daily budget, ₹1,500 per purchase,
    three allowed merchants.
    """

    user: Signer
    agent: Signer
    merchant: Signer
    amount: int = inr(1299)
    budget: int = inr(5000)
    per_txn_max: int = inr(1500)
    allowed_payees: list[str] = field(default_factory=lambda: [MERCHANT_ID, "m_lumen"])
    now: datetime = field(default_factory=utcnow)

    open_payment_jws: str = field(init=False)
    open_payment: PaymentMandateContents = field(init=False)
    checkout_jws: str = field(init=False)

    def __post_init__(self) -> None:
        self.open_payment = open_payment_mandate(
            budget=self.budget,
            amount_max=self.per_txn_max,
            allowed_payees=self.allowed_payees,
            not_before=self.now - timedelta(minutes=1),
            not_after=self.now + timedelta(hours=24),
            # The user delegates to the AGENT's key: only that agent may present
            # transactions under this authorisation.
            cnf=self.agent.cnf,
        )
        self.open_payment_jws = self.user.sign(self.open_payment, ttl_seconds=86400, now=self.now)
        self.checkout_jws = self.merchant.sign(
            closed_checkout_mandate(cart=cart(self.amount)), ttl_seconds=900, now=self.now
        )

    # -- what the agent presents -------------------------------------------

    def closed_payment(
        self,
        *,
        amount: int | None = None,
        payee: str = MERCHANT_ID,
        instrument: str = "upi",
        checkout_jws: str | None = None,
        open_jws: str | None = None,
        execution_date: datetime | None = None,
        nonce: str | None = None,
        mandate_id: str | None = None,
    ) -> PaymentMandateContents:
        return closed_payment_mandate(
            payee=payee,
            payee_name=MERCHANT_NAME,
            amount=self.amount if amount is None else amount,
            payment_instrument=instrument,
            checkout_hash=checkout_hash(checkout_jws or self.checkout_jws),
            open_mandate_jws=open_jws or self.open_payment_jws,
            execution_date=execution_date or self.now,
            nonce=nonce,
            mandate_id=mandate_id,
        )

    def present(self, signer: Signer | None = None, **kwargs: object) -> str:
        """Sign a closed payment mandate as the agent (or whoever is given)."""
        contents = self.closed_payment(**kwargs)  # type: ignore[arg-type]
        return (signer or self.agent).sign(contents, ttl_seconds=600, now=self.now)

    def present_open(self) -> str:
        """The agent presenting only its standing authorisation — an escalation."""
        return self.open_payment_jws
