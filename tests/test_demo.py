"""The demo, and the claim that its numbers are real.

The most important tests in this file are the ones that *change the world and
check the report changes with it*. A demo whose output is the same whether or not
the code works is a screenshot.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from ap2_min.models import inr
from demo.batch import PLAN, REPORT_PATH, Report, main_async, measure, run_batch
from gateway.audit import Event
from gateway.bootstrap import Gateway, build_gateway
from gateway.razorpay_client import FakeRail
from gateway.trusted_surface import HeldRequest
from shopping_agent.agent import (
    STATUS_DECLINED_STOCK,
    STATUS_HUMAN_DENIED,
    STATUS_PAID,
)

EXPECTED_LINE = (
    "6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained"
)


@pytest.fixture
def demo_gateway() -> Iterator[Gateway]:
    gateway = build_gateway(use_llm=False, sleep=lambda _seconds: None)
    try:
        yield gateway
    finally:
        gateway.close()


# ---------------------------------------------------------------------------
# The headline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_batch_produces_the_exact_report_line(
    demo_gateway: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    results, _ = await run_batch(demo_gateway, quiet=True)
    report = measure(demo_gateway, results)
    assert report.line() == EXPECTED_LINE


@pytest.mark.asyncio
async def test_the_six_attempts_end_the_way_the_scenario_says(
    demo_gateway: Gateway,
) -> None:
    results, shopper = await run_batch(demo_gateway, quiet=True)

    assert [r.status for r in results] == [
        STATUS_PAID,  # 1 clean buy, after re-planning past a fake SKU
        STATUS_PAID,  # 2 clean buy
        STATUS_HUMAN_DENIED,  # 3 over the cap, a human said no
        STATUS_PAID,  # 4 UPI declined, recovered on a payment link
        STATUS_PAID,  # 5 clean buy
        STATUS_DECLINED_STOCK,  # 6 sold out between checkout and payment
    ]
    assert results[0].replans == 1
    assert results[2].escalated
    assert results[3].recovered
    assert results[3].attempts == 2
    assert shopper.decisions == [(shopper.decisions[0][0], False)]


@pytest.mark.asyncio
async def test_the_batch_spans_all_three_merchants(demo_gateway: Gateway) -> None:
    results, _ = await run_batch(demo_gateway, quiet=True)
    assert {r.merchant for r in results if r.merchant} == {
        "StrideFit Sportswear",
        "Lumen Home & Kitchen",
        "PixelByte Electronics",
    }


# ---------------------------------------------------------------------------
# The numbers are measured, not written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaking_the_rail_changes_the_report(demo_gateway: Gateway) -> None:
    """If the report were hardcoded, this test could not fail.

    Make every payment decline. If `paid` still came out 4, the number would be a
    constant rather than a measurement.
    """
    assert isinstance(demo_gateway.rail, FakeRail)
    demo_gateway.rail.decline(times=None)

    results, _ = await run_batch(demo_gateway, quiet=True)
    report = measure(demo_gateway, results)

    assert report.paid == 0
    assert report.recovered == 0
    assert report.line() != EXPECTED_LINE
    assert demo_gateway.rail.captured_total() == 0


@pytest.mark.asyncio
async def test_a_shopper_who_approves_changes_the_report(demo_gateway: Gateway) -> None:
    """The human's decision is an input. Change it and the outcome changes."""
    import demo.batch as batch

    def approve_everything(request: HeldRequest) -> bool:
        return True

    original = batch.approval_policy
    batch.approval_policy = approve_everything
    try:
        results, _ = await run_batch(demo_gateway, quiet=True)
    finally:
        batch.approval_policy = original

    report = measure(demo_gateway, results)
    assert report.human_denied == 0
    assert report.paid == 5, "the ₹4,999 shoe went through once a human said yes"
    assert results[2].status == STATUS_PAID
    assert results[2].charged_amount == inr(4999)


@pytest.mark.asyncio
async def test_removing_the_stock_event_changes_attempt_six(demo_gateway: Gateway) -> None:
    """Without the concurrent buyer, attempt 6 is an ordinary purchase."""
    import demo.batch as batch

    original = batch._interleave_for
    batch._interleave_for = lambda *_args, **_kwargs: None  # type: ignore[assignment]
    try:
        results, _ = await run_batch(demo_gateway, quiet=True)
    finally:
        batch._interleave_for = original  # type: ignore[assignment]

    assert results[5].status == STATUS_PAID
    assert measure(demo_gateway, results).paid == 5


@pytest.mark.asyncio
async def test_the_report_is_derived_from_signed_receipts(demo_gateway: Gateway) -> None:
    """`paid` counts receipts, not intentions."""
    results, _ = await run_batch(demo_gateway, quiet=True)
    paid = [r for r in results if r.status == STATUS_PAID]
    assert len(paid) == 4
    for result in paid:
        assert result.receipt is not None
        assert result.receipt["status"] == "captured"
        assert result.receipt_jws, "every paid attempt carries a verifiable receipt"
        assert result.charged_amount == result.amount


@pytest.mark.asyncio
async def test_the_money_reconciles_three_ways(demo_gateway: Gateway) -> None:
    """measure() raises if the rail, the ledger and the receipts disagree."""
    results, _ = await run_batch(demo_gateway, quiet=True)
    assert isinstance(demo_gateway.rail, FakeRail)

    receipts = sum(r.charged_amount for r in results)
    assert receipts == inr(1299) + inr(899) + inr(1199) + inr(699)
    assert demo_gateway.ledger.total_captured() == receipts
    assert demo_gateway.rail.captured_total() == receipts

    # And measure() actually checks it, rather than merely reporting one view.
    demo_gateway.ledger.record_spend(
        open_mandate_id="pmo_phantom",
        payment_mandate_id="pm_phantom",
        amount=inr(1),
        currency="INR",
        payee="m_stridefit",
    )
    with pytest.raises(AssertionError, match="reconciliation failed"):
        measure(demo_gateway, results)


@pytest.mark.asyncio
async def test_unauthorised_spend_is_zero_because_every_capture_traces_to_an_allow(
    demo_gateway: Gateway,
) -> None:
    results, _ = await run_batch(demo_gateway, quiet=True)
    report = measure(demo_gateway, results)
    assert report.unauthorised_spend == 0

    allows = [
        row
        for row in demo_gateway.audit.rows(event=Event.DECISION)
        if row.payload["outcome"] == "ALLOW"
    ]
    captures = demo_gateway.audit.rows(event=Event.PAYMENT_CAPTURED)
    assert len(captures) <= len(allows), "no capture without an ALLOW before it"
    assert len(captures) == 4


@pytest.mark.asyncio
async def test_every_attempt_is_explained(demo_gateway: Gateway) -> None:
    results, _ = await run_batch(demo_gateway, quiet=True)
    assert measure(demo_gateway, results).actions_explained == "6/6"
    for result in results:
        assert result.human_reason.strip(), f"{result.goal.label} has no explanation"
    for row in demo_gateway.audit.rows():
        assert row.human_reason, f"{row.event} has no explanation"


@pytest.mark.asyncio
async def test_the_audit_chain_survives_the_whole_batch(demo_gateway: Gateway) -> None:
    await run_batch(demo_gateway, quiet=True)
    chain = demo_gateway.audit.verify_chain()
    assert chain.ok, chain.reason
    assert chain.rows_checked > 100


@pytest.mark.asyncio
async def test_the_batch_is_deterministic() -> None:
    """Two runs, same numbers. A demo you cannot re-run is a demo you cannot trust."""
    lines = []
    for _ in range(2):
        gateway = build_gateway(use_llm=False, sleep=lambda _s: None)
        try:
            results, _ = await run_batch(gateway, quiet=True)
            lines.append(measure(gateway, results).line())
        finally:
            gateway.close()
    assert lines[0] == lines[1] == EXPECTED_LINE


# ---------------------------------------------------------------------------
# The plan and the report file
# ---------------------------------------------------------------------------


def test_the_plan_stays_inside_the_daily_budget(demo_gateway: Gateway) -> None:
    """The scenario has to be arithmetically possible, or attempt 6 fails for the
    wrong reason."""
    catalogue = demo_gateway.catalog
    spending = [
        catalogue.get(goal.prefer_sku).price
        for goal in PLAN
        if goal.prefer_sku and catalogue.get(goal.prefer_sku).price <= inr(1500)
    ]
    assert sum(spending) <= demo_gateway.policy.standing_authorisation.daily_budget


def test_the_over_cap_goal_really_is_over_the_cap(demo_gateway: Gateway) -> None:
    price = demo_gateway.catalog.get("SF-RUN-004").price
    assert price > demo_gateway.policy.standing_authorisation.per_txn_max


def test_the_sold_out_goal_would_otherwise_have_been_affordable(
    demo_gateway: Gateway,
) -> None:
    """Attempt 6 must fail on stock and stock alone.

    If it were also over budget the demo would be ambiguous about which guard
    fired.
    """
    from demo.batch import SOLD_OUT_SKU

    price = demo_gateway.catalog.get(SOLD_OUT_SKU).price
    standing = demo_gateway.policy.standing_authorisation
    spent = inr(1299) + inr(899) + inr(1199) + inr(699)
    assert price <= standing.per_txn_max
    assert spent + price <= standing.daily_budget


@pytest.mark.asyncio
async def test_the_cli_writes_report_json() -> None:
    exit_code = await main_async(["--json"])
    assert exit_code == 0

    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert report["attempts"] == 6
    assert report["paid"] == 4
    assert report["human_denied"] == 1
    assert report["recovered"] == 1
    assert report["unauthorised_spend"] == 0
    assert report["actions_explained"] == "6/6"
    assert report["line"] == EXPECTED_LINE
    assert report["audit_chain_intact"] is True
    assert report["rail"] == "fake"
    assert len(report["attempts_detail"]) == 6


def test_the_report_line_format_is_exact() -> None:
    """The video reads this line. It must not drift."""
    report = Report(
        attempts=6,
        paid=4,
        human_denied=1,
        recovered=1,
        unauthorised_spend=0,
        actions_explained="6/6",
    )
    assert report.line() == EXPECTED_LINE


# ---------------------------------------------------------------------------
# The offline claim, proved rather than asserted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_batch_opens_no_sockets(
    demo_gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Zero network" is a claim worth breaking the process over.

    Sabotage `socket.socket` so that constructing one raises, then run the whole
    batch. Six purchases, a human gate, a recovery, an audit chain — and not one
    socket. This is what makes the demo runnable on a plane, in a review, or on a
    machine that has never had an API key.
    """
    import socket

    class NoNetwork(socket.socket):  # type: ignore[misc]
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("the demo opened a socket; it is supposed to be offline")

    monkeypatch.setattr(socket, "socket", NoNetwork)

    results, _ = await run_batch(demo_gateway, quiet=True)
    assert measure(demo_gateway, results).line() == EXPECTED_LINE
