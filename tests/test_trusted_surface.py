"""The human gate.

Two things are being tested, and the second matters more than the first:

1. That approval and denial work, and that denial charges nothing.
2. That an **approval is not an unlock**. The mandate a Yes produces authorises
   exactly one amount, at exactly one merchant, for exactly one basket, for a few
   minutes. Everything below pushes on that boundary.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ap2_min.models import (
    AllowedPayeesConstraint,
    AmountRangeConstraint,
    BudgetConstraint,
    ReferenceConstraint,
    inr,
)
from ap2_min.roles import ROLE_USER
from ap2_min.vct import VCT_CHECKOUT_CLOSED, VCT_PAYMENT_CLOSED, VCT_PAYMENT_OPEN
from gateway.app import create_app
from gateway.audit import Event
from gateway.bootstrap import Gateway
from gateway.mandates import checkout_hash, load_payment_mandate, verify_jws
from gateway.trusted_surface import ONE_TIME_MANDATE_TTL_SECONDS
from shopping_agent.human import SimulatedShopper, always_approve, always_deny


def _hold(wired: Gateway, sku: str = "SF-RUN-004") -> dict[str, Any]:
    """Drive a purchase far enough to raise the gate, and return the response."""
    merchant = wired.merchant
    cart = merchant.assemble_cart([{"sku": sku, "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    response = merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)
    assert response["error"] == "unresolved_constraint"
    return response


# ---------------------------------------------------------------------------
# Raising the gate
# ---------------------------------------------------------------------------


def test_an_over_limit_cart_raises_a_hold(wired: Gateway) -> None:
    response = _hold(wired)
    held = wired.trusted_surface.get(response["hold_id"])
    assert held.status == "pending"
    assert held.amount == inr(4999)
    assert held.constraint_code == "checkout.amount_exceeds_standing_limit"
    assert wired.audit.rows(event=Event.GATE_REQUESTED)


def test_the_page_shows_the_merchant_signed_amount(wired: Gateway) -> None:
    """The number a person reads is the number the merchant signed.

    Not a summary of it, not a model's paraphrase of it — the integer out of the
    Checkout Mandate, rendered.
    """
    response = _hold(wired)
    page = wired.trusted_surface.render(wired.trusted_surface.get(response["hold_id"]))
    assert "₹4,999.00" in page
    assert "StrideFit Sportswear" in page
    assert "Marathon Elite Carbon" in page
    assert "SF-RUN-004" in page
    assert "This page contains no AI" in page


def test_the_page_escapes_what_it_renders(wired: Gateway) -> None:
    """Held requests carry merchant-supplied strings; none of them are markup."""
    response = _hold(wired)
    held = wired.trusted_surface.get(response["hold_id"])
    held.human_reason = "<script>alert('xss')</script>"
    page = wired.trusted_surface.render(held)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_the_page_explains_that_approval_is_not_an_unlock(wired: Gateway) -> None:
    response = _hold(wired)
    page = wired.trusted_surface.render(wired.trusted_surface.get(response["hold_id"]))
    assert "Your standing limit does not change." in page


# ---------------------------------------------------------------------------
# Denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_denial_charges_nothing_and_issues_no_mandate(wired: Gateway) -> None:
    response = _hold(wired)
    shopper = SimulatedShopper(wired.trusted_surface, policy=always_deny)
    decision = await shopper.await_decision(response["hold_id"], approval_url="")

    assert decision["status"] == "denied"
    assert decision["payment_mandate_jws"] is None
    assert decision["checkout_mandate_jws"] is None
    assert wired.ledger.total_captured() == 0
    assert wired.audit.rows(event=Event.GATE_DENIED)
    assert not wired.audit.rows(event=Event.GATE_APPROVED)


def test_a_decision_is_final(wired: Gateway) -> None:
    """A double-clicked Approve must not mint a second mandate."""
    response = _hold(wired)
    first = wired.trusted_surface.decide(response["hold_id"], approve=True)
    second = wired.trusted_surface.decide(response["hold_id"], approve=False)
    assert second.status == "approved"
    assert second.payment_mandate_jws == first.payment_mandate_jws
    assert len(wired.audit.rows(event=Event.GATE_APPROVED)) == 1


# ---------------------------------------------------------------------------
# Approval: the narrowest possible mandate
# ---------------------------------------------------------------------------


def test_approval_mints_a_mandate_scoped_to_exactly_this_purchase(wired: Gateway) -> None:
    """The headline property. A Yes to one thing is a Yes to one thing."""
    response = _hold(wired)
    approved = wired.trusted_surface.decide(response["hold_id"], approve=True)
    assert approved.payment_mandate_jws is not None

    closed, _ = load_payment_mandate(
        approved.payment_mandate_jws, wired.keyring, expected_role=ROLE_USER
    )
    assert closed.vct == VCT_PAYMENT_CLOSED
    assert closed.payment_amount == inr(4999)
    assert closed.open_mandate_jws is not None

    scoped, _ = load_payment_mandate(
        closed.open_mandate_jws, wired.keyring, expected_role=ROLE_USER
    )
    assert scoped.vct == VCT_PAYMENT_OPEN

    amount_range = scoped.constraint("payment.amount_range")
    assert isinstance(amount_range, AmountRangeConstraint)
    assert amount_range.min == amount_range.max == inr(4999), "one amount, not a range"

    budget = scoped.constraint("payment.budget")
    assert isinstance(budget, BudgetConstraint)
    assert budget.max == inr(4999), "funds one payment and can never fund a second"

    payees = scoped.constraint("payment.allowed_payees")
    assert isinstance(payees, AllowedPayeesConstraint)
    assert payees.ids == ["m_stridefit"], "one merchant"

    reference = scoped.constraint("payment.reference")
    assert isinstance(reference, ReferenceConstraint)
    assert reference.conditional_transaction_id == checkout_hash(approved.checkout_jws)


def test_the_one_time_mandate_expires_in_minutes(wired: Gateway) -> None:
    from gateway.mandates import decode_unverified

    response = _hold(wired)
    approved = wired.trusted_surface.decide(response["hold_id"], approve=True)
    assert approved.payment_mandate_jws is not None
    claims = decode_unverified(approved.payment_mandate_jws)
    assert claims["exp"] - claims["iat"] == ONE_TIME_MANDATE_TTL_SECONDS


def test_approval_does_not_touch_the_standing_authorisation(wired: Gateway) -> None:
    """The buyer's ₹1,500 cap is exactly where it was before they said yes."""
    before = wired.open_payment_jws
    response = _hold(wired)
    wired.trusted_surface.decide(response["hold_id"], approve=True)
    assert wired.open_payment_jws == before

    standing, _ = load_payment_mandate(
        wired.open_payment_jws, wired.keyring, expected_role=ROLE_USER
    )
    ceiling = standing.constraint("payment.amount_range")
    assert isinstance(ceiling, AmountRangeConstraint)
    assert ceiling.max == inr(1500)


def test_approval_also_signs_the_buyers_cart_confirmation(wired: Gateway) -> None:
    """AP2's human-present cart confirmation: the buyer's own signature on the basket."""
    from ap2_min.models import CheckoutMandateContents
    from gateway.mandates import verify_and_load

    response = _hold(wired)
    approved = wired.trusted_surface.decide(response["hold_id"], approve=True)
    assert approved.checkout_mandate_jws is not None
    contents, _ = verify_and_load(
        approved.checkout_mandate_jws,
        wired.keyring,
        CheckoutMandateContents,
        expected_role=ROLE_USER,
    )
    assert contents.vct == VCT_CHECKOUT_CLOSED
    assert contents.cart is not None
    assert contents.cart.total == inr(4999)
    assert checkout_hash(approved.checkout_jws) in contents.delegate_chain


@pytest.mark.asyncio
async def test_an_approved_purchase_goes_through_and_charges_once(wired: Gateway) -> None:
    """The whole failure-mode-8 path: escalate, approve, pay, exactly once."""
    from gateway.razorpay_client import FakeRail

    merchant = wired.merchant
    cart = merchant.assemble_cart([{"sku": "SF-RUN-004", "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    unresolved = merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)

    shopper = SimulatedShopper(wired.trusted_surface, policy=always_approve)
    decision = await shopper.await_decision(unresolved["hold_id"], approval_url="")
    assert decision["status"] == "approved"

    confirmed = merchant.complete_checkout(
        checkout["checkout_id"], decision["checkout_mandate_jws"]
    )
    assert confirmed["status"] == "confirmed"

    paid = merchant.initiate_payment(checkout["checkout_id"], decision["payment_mandate_jws"])
    assert paid["status"] == "captured"
    assert paid["payment_receipt"]["amount"] == inr(4999)

    assert isinstance(wired.rail, FakeRail)
    assert wired.rail.captured_total() == inr(4999)

    # Presenting the same approved mandate again returns the same receipt.
    again = merchant.initiate_payment(checkout["checkout_id"], decision["payment_mandate_jws"])
    assert again["replayed"] is True
    assert wired.rail.captured_total() == inr(4999)


def test_a_one_time_mandate_cannot_be_reused_for_a_different_basket(wired: Gateway) -> None:
    """payment.reference pins it. A second cart at the same price will not do."""
    merchant = wired.merchant
    first = merchant.assemble_cart([{"sku": "SF-RUN-004", "qty": 1}])["cart"]
    first_checkout = merchant.create_checkout(first["cart_id"])
    unresolved = merchant.complete_checkout(first_checkout["checkout_id"], wired.open_checkout_jws)
    approved = wired.trusted_surface.decide(unresolved["hold_id"], approve=True)
    assert approved.payment_mandate_jws is not None

    # A second, identical-priced basket, properly approved on its own.
    second = merchant.assemble_cart([{"sku": "SF-RUN-004", "qty": 1}])["cart"]
    second_checkout = merchant.create_checkout(second["cart_id"])
    second_unresolved = merchant.complete_checkout(
        second_checkout["checkout_id"], wired.open_checkout_jws
    )
    second_approved = wired.trusted_surface.decide(second_unresolved["hold_id"], approve=True)
    merchant.complete_checkout(
        second_checkout["checkout_id"], second_approved.checkout_mandate_jws or ""
    )

    # Now present the FIRST basket's mandate against the SECOND basket. Same
    # amount, same merchant, same buyer — a different cart.
    response = merchant.initiate_payment(
        second_checkout["checkout_id"], approved.payment_mandate_jws
    )
    assert response["error"] == "denied"
    assert response["code"] == "payment.reference.mismatch"
    assert wired.ledger.total_captured() == 0


def test_an_expired_hold_lapses_rather_than_lingering(wired: Gateway) -> None:
    from datetime import timedelta

    from gateway.mandates import utcnow

    response = _hold(wired)
    held = wired.trusted_surface.get(response["hold_id"])
    held.created_ts = (utcnow() - timedelta(hours=2)).isoformat()

    lapsed = wired.trusted_surface.get(response["hold_id"])
    assert lapsed.status == "expired"
    assert wired.audit.rows(event=Event.GATE_EXPIRED)
    assert wired.trusted_surface.decide(response["hold_id"], approve=True).status == "expired"


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


def test_the_approval_page_is_served_and_the_form_works(wired: Gateway) -> None:
    client = TestClient(create_app(wired))
    response = _hold(wired)
    hold_id = response["hold_id"]

    page = client.get(f"/trusted-surface/{hold_id}")
    assert page.status_code == 200
    assert "Approve ₹4,999.00 once" in page.text

    posted = client.post(f"/trusted-surface/{hold_id}/decision", data={"decision": "deny"})
    assert posted.status_code == 200
    assert "Declined" in posted.text
    assert wired.trusted_surface.get(hold_id).status == "denied"


def test_the_status_endpoint_is_read_only_for_the_agent(wired: Gateway) -> None:
    client = TestClient(create_app(wired))
    hold_id = _hold(wired)["hold_id"]
    body = client.get(f"/trusted-surface/{hold_id}/status").json()
    assert body["status"] == "pending"
    assert body["payment_mandate_jws"] is None
    # There is no route an agent can call to change this.
    assert client.post(f"/trusted-surface/{hold_id}/status").status_code == 405


def test_an_unknown_hold_is_a_404(wired: Gateway) -> None:
    client = TestClient(create_app(wired))
    assert client.get("/trusted-surface/gate_nope").status_code == 404


def test_a_nonsense_decision_is_rejected(wired: Gateway) -> None:
    client = TestClient(create_app(wired))
    hold_id = _hold(wired)["hold_id"]
    response = client.post(f"/trusted-surface/{hold_id}/decision", data={"decision": "approve-ish"})
    assert response.status_code == 400


def test_the_trusted_surface_signs_as_the_buyer(wired: Gateway) -> None:
    """It models the buyer's own device. Nothing else may hold that key."""
    response = _hold(wired)
    approved = wired.trusted_surface.decide(response["hold_id"], approve=True)
    assert approved.payment_mandate_jws is not None
    claims = verify_jws(approved.payment_mandate_jws, wired.keyring, expected_role=ROLE_USER)
    assert claims["iss"] == wired.user.kid
