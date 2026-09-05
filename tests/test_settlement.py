"""Settlement: what happens after the verifier says ALLOW.

Every test here corresponds to a bug found by probing the system rather than by
reading it. They are grouped separately from `test_payments.py` because they
share one theme: **once money has moved, the code that runs next may not fail.**

The verifier is allowed to refuse. The rail is allowed to decline. Settlement —
decrementing stock, writing the receipt, recording the spend — is not, because by
then the buyer has already been charged and is owed proof of it.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from ap2_min.builders import closed_payment_mandate
from ap2_min.models import inr
from gateway.audit import Event
from gateway.bootstrap import Gateway, build_gateway
from gateway.db import MEMORY
from gateway.mandates import utcnow
from gateway.razorpay_client import FakeRail
from merchant.checkout import Catalog, CheckoutStore, OutOfStock


def _prepare(gateway: Gateway, sku: str = "SF-RUN-001") -> tuple[str, str]:
    """A confirmed checkout and a valid mandate for it."""
    merchant = gateway.merchant
    cart = merchant.assemble_cart([{"sku": sku, "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    merchant.complete_checkout(checkout["checkout_id"], gateway.open_checkout_jws)
    now = utcnow()
    contents = closed_payment_mandate(
        payee=cart["merchant_id"],
        payee_name=cart["merchant_name"],
        amount=cart["total"],
        payment_instrument="upi",
        checkout_hash=checkout["checkout_hash"],
        open_mandate_jws=gateway.open_payment_jws,
        execution_date=now,
    )
    return checkout["checkout_id"], gateway.agent.sign(contents, ttl_seconds=600, now=now)


# ===========================================================================
# One checkout is one purchase
# ===========================================================================


def test_a_settled_checkout_refuses_a_second_payment_mandate(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """The double-charge this suite did not previously catch.

    Mandate-level idempotency only recognises the *same* mandate. A second,
    freshly signed mandate for an already-paid checkout has a different id, a
    different nonce, and passes every verifier check — same payee, same amount,
    within budget, correct checkout hash. Before this guard existed it charged
    the buyer twice for one basket.
    """
    checkout_id, first = _prepare(wired)
    assert wired.merchant.initiate_payment(checkout_id, first)["status"] == "captured"

    _, second = _prepare(wired)  # a different mandate, same price
    response = wired.merchant.initiate_payment(checkout_id, second)

    assert response["error"] == "checkout.already_settled"
    assert response["charged"] is False
    assert fake_rail.captured_total() == inr(1299), "charged exactly once"
    assert len(fake_rail.orders()) == 1, "no second order was created"


def test_the_refusal_names_the_mandate_that_settled_it(wired: Gateway) -> None:
    """ "Already paid" is not actionable. "Paid by pm_x" is."""
    from gateway.mandates import load_payment_mandate

    checkout_id, mandate = _prepare(wired)
    wired.merchant.initiate_payment(checkout_id, mandate)
    settling, _ = load_payment_mandate(mandate, wired.keyring)

    _, second = _prepare(wired)
    response = wired.merchant.initiate_payment(checkout_id, second)

    assert response["settled_by"] == settling.mandate_id
    assert "already been paid" in response["message"]


def test_the_original_mandate_still_returns_its_receipt(wired: Gateway) -> None:
    """The guard must not break idempotent replay, which runs before it."""
    checkout_id, mandate = _prepare(wired)
    first = wired.merchant.initiate_payment(checkout_id, mandate)
    again = wired.merchant.initiate_payment(checkout_id, mandate)

    assert again["replayed"] is True
    assert again["payment_receipt"]["receipt_id"] == first["payment_receipt"]["receipt_id"]


def test_the_refusal_is_audited(wired: Gateway) -> None:
    checkout_id, first = _prepare(wired)
    wired.merchant.initiate_payment(checkout_id, first)
    _, second = _prepare(wired)
    wired.merchant.initiate_payment(checkout_id, second)

    rows = wired.audit.rows(event=Event.CHECKOUT_ALREADY_SETTLED)
    assert len(rows) == 1
    assert "one checkout is one purchase" in (rows[0].human_reason or "")
    assert wired.audit.verify_chain().ok


def test_a_deferred_payment_can_still_be_completed(wired: Gateway, fake_rail: FakeRail) -> None:
    """A deferral is not a settlement, so the guard must not block the retry.

    This is the regression that matters most: the settled-checkout guard sits
    close to the deferred-retry path, and getting it wrong would strand every
    payment the circuit breaker deferred.
    """
    checkout_id, mandate = _prepare(wired)
    fake_rail.timeout(times=None)
    assert wired.merchant.initiate_payment(checkout_id, mandate)["status"] == "deferred"

    fake_rail.reset_rules()
    wired.breaker.record_success()

    assert wired.merchant.initiate_payment(checkout_id, mandate)["status"] == "captured"
    assert fake_rail.captured_total() == inr(1299)


def test_a_failed_payment_does_not_settle_the_checkout(wired: Gateway, fake_rail: FakeRail) -> None:
    """Recovery exhausted means nothing was paid, so nothing is settled."""
    checkout_id, mandate = _prepare(wired)
    fake_rail.decline(times=None)
    assert wired.merchant.initiate_payment(checkout_id, mandate)["status"] == "failed"

    assert not wired.store.checkout(checkout_id).stock_committed
    assert wired.store.checkout(checkout_id).settled_payment_mandate_id is None


# ===========================================================================
# Settlement may not raise once money has moved
# ===========================================================================


def test_taking_more_stock_than_exists_never_raises() -> None:
    """`take` is the post-capture primitive. Raising there is not an option."""
    catalog = Catalog()
    catalog.set_stock("SF-RUN-001", 1)

    remaining, shortfall = catalog.take("SF-RUN-001", 3)

    assert remaining == 0, "clamped, never negative"
    assert shortfall == 2
    assert catalog.stock("SF-RUN-001") == 0


def test_decrement_still_raises_because_it_runs_before_payment() -> None:
    """The two primitives are different on purpose.

    Before money moves, refusing is correct. After it moves, refusing is not an
    available answer. Collapsing them into one would lose that distinction.
    """
    catalog = Catalog()
    catalog.set_stock("SF-RUN-001", 1)
    with pytest.raises(OutOfStock):
        catalog.decrement("SF-RUN-001", 3)
    assert catalog.stock("SF-RUN-001") == 1, "a refused decrement takes nothing"


def test_committing_more_than_the_shelf_holds_reports_a_shortfall() -> None:
    catalog = Catalog()
    store = CheckoutStore(catalog)
    cart = store.assemble_cart([{"sku": "SF-RUN-001", "qty": 2}], ship_to_pincode="560001")
    record = store.open_checkout(cart)
    catalog.set_stock("SF-RUN-001", 1)  # a concurrent buyer took one

    commit = store.commit_stock(record.checkout_id, payment_mandate_id="pm_x")

    assert commit.oversold
    assert commit.shortfall == {"SF-RUN-001": 1}
    assert commit.remaining == {"SF-RUN-001": 0}
    assert catalog.stock("SF-RUN-001") == 0


def test_concurrent_captures_never_crash_and_every_buyer_gets_a_receipt(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """The crash this file exists for.

    Five checkouts each pass the pre-payment stock re-check while stock is still
    positive, then all capture simultaneously. Before the fix the fifth
    `commit_stock` raised OutOfStock *after* the rail had taken the money — the
    buyer was charged, the merchant's code was in a traceback, and the agent held
    no receipt. The worst possible ordering.

    Overselling is a fulfilment problem. Crashing after taking money is a bug.
    """
    wired.catalog.set_stock("SF-RUN-001", 1)
    prepared = [_prepare(wired) for _ in range(5)]

    barrier = threading.Barrier(len(prepared))
    crashes: list[str] = []

    def pay(item: tuple[str, str]) -> dict[str, Any]:
        checkout_id, mandate = item
        barrier.wait()
        try:
            return wired.merchant.initiate_payment(checkout_id, mandate)
        except Exception as exc:  # noqa: BLE001 — a crash here is the finding
            crashes.append(f"{type(exc).__name__}: {exc}")
            return {}

    with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
        responses = list(pool.map(pay, prepared))

    assert crashes == [], f"settlement crashed after capture: {crashes}"
    captured = [r for r in responses if r.get("status") == "captured"]
    assert captured, "the scenario did not actually capture anything"
    for response in captured:
        assert response["payment_receipt_jws"], "a charged buyer got no receipt"

    assert wired.catalog.stock("SF-RUN-001") >= 0, "stock went negative"
    assert wired.audit.verify_chain().ok


def test_an_oversell_is_recorded_loudly_rather_than_clamped_silently(
    wired: Gateway,
) -> None:
    """Clamping without a record would hide a real inventory problem.

    The shelf is emptied in the exact window the race occupies: after the last
    pre-payment re-check has passed, before settlement takes the units. Emptying
    it any earlier just makes the re-check refuse, which is the *other* (already
    tested) behaviour.
    """
    checkout_id, mandate = _prepare(wired)
    original_commit = wired.store.commit_stock

    def a_concurrent_buyer_takes_the_last_one(
        cid: str, *, payment_mandate_id: str | None = None
    ) -> Any:
        wired.catalog.set_stock("SF-RUN-001", 0)
        return original_commit(cid, payment_mandate_id=payment_mandate_id)

    wired.store.commit_stock = a_concurrent_buyer_takes_the_last_one  # type: ignore[method-assign,assignment]
    response = wired.merchant.initiate_payment(checkout_id, mandate)

    assert response["status"] == "captured", "the payment was authorised and did capture"
    assert response["payment_receipt_jws"], "a charged buyer is owed a receipt"

    rows = wired.audit.rows(event=Event.STOCK_OVERSOLD)
    assert len(rows) == 1
    assert "backorder or a refund" in (rows[0].human_reason or "")
    assert rows[0].payload["shortfall"] == {"SF-RUN-001": 1}
    assert rows[0].payload["receipt_id"] == response["payment_receipt"]["receipt_id"]
    assert wired.audit.verify_chain().ok


def test_a_normal_capture_records_no_oversell(wired: Gateway) -> None:
    """The loud path must stay quiet when nothing is wrong."""
    checkout_id, mandate = _prepare(wired)
    assert wired.merchant.initiate_payment(checkout_id, mandate)["status"] == "captured"
    assert wired.audit.rows(event=Event.STOCK_OVERSOLD) == []
    assert wired.audit.rows(event=Event.STOCK_DECREMENTED)


def test_committing_stock_twice_still_takes_the_units_once() -> None:
    """A duplicate receipt is normal; a double decrement is not."""
    catalog = Catalog()
    store = CheckoutStore(catalog)
    before = catalog.stock("SF-RUN-001")
    cart = store.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], ship_to_pincode="560001")
    record = store.open_checkout(cart)

    first = store.commit_stock(record.checkout_id, payment_mandate_id="pm_a")
    second = store.commit_stock(record.checkout_id, payment_mandate_id="pm_b")

    assert not first.already_committed
    assert second.already_committed
    assert catalog.stock("SF-RUN-001") == before - 1
    assert record.settled_payment_mandate_id == "pm_a", "the first mandate keeps the credit"


# ===========================================================================
# The budget still holds across all of this
# ===========================================================================


def test_the_daily_budget_is_never_exceeded_across_many_purchases() -> None:
    """Independent of the guards above: the ceiling is the ceiling."""
    gateway = build_gateway(db_path=MEMORY, use_llm=False, sleep=lambda _s: None)
    try:
        budget = gateway.policy.standing_authorisation.daily_budget
        for _ in range(8):
            checkout_id, mandate = _prepare(gateway)
            gateway.merchant.initiate_payment(checkout_id, mandate)
        assert gateway.ledger.total_captured() <= budget
        assert isinstance(gateway.rail, FakeRail)
        assert gateway.rail.captured_total() == gateway.ledger.total_captured()
    finally:
        gateway.close()


# ===========================================================================
# A spent mandate answers for its own checkout and no other
# ===========================================================================


def test_a_spent_mandate_is_refused_against_a_different_checkout(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """No double charge, but the old behaviour was arguably worse than one.

    Presenting a mandate that settled checkout A against checkout B returned A's
    receipt with `status: captured`. No money moved twice — but the agent was told
    B was paid when it was not, and a merchant acting on that ships goods against
    a receipt belonging to a different order. A false positive on "did this get
    paid" is a real loss, quieter than a double charge and harder to notice.
    """
    first_id, mandate = _prepare(wired)
    second_id, _ = _prepare(wired)
    assert wired.merchant.initiate_payment(first_id, mandate)["status"] == "captured"

    response = wired.merchant.initiate_payment(second_id, mandate)

    assert response["error"] == "mandate.spent_on_another_checkout"
    assert response["charged"] is False
    assert "status" not in response, "it must not look like a capture"
    assert not wired.store.checkout(second_id).stock_committed
    assert fake_rail.captured_total() == inr(1299)


def test_the_replay_path_still_works_for_the_mandates_own_checkout(
    wired: Gateway,
) -> None:
    """The binding check must not break idempotent replay, which sits beside it."""
    checkout_id, mandate = _prepare(wired)
    first = wired.merchant.initiate_payment(checkout_id, mandate)
    again = wired.merchant.initiate_payment(checkout_id, mandate)

    assert again["replayed"] is True
    assert again["payment_receipt"]["receipt_id"] == first["payment_receipt"]["receipt_id"]


def test_the_mismatch_is_audited_with_both_hashes(wired: Gateway) -> None:
    """An operator needs to see which two orders were confused."""
    first_id, mandate = _prepare(wired)
    second_id, _ = _prepare(wired)
    wired.merchant.initiate_payment(first_id, mandate)
    wired.merchant.initiate_payment(second_id, mandate)

    rows = wired.audit.rows(event=Event.MANDATE_WRONG_CHECKOUT)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["settled_checkout_hash"] != payload["presented_checkout_hash"]
    assert payload["checkout_id"] == second_id
    assert "says nothing about" in (rows[0].human_reason or "")
    assert wired.audit.verify_chain().ok
