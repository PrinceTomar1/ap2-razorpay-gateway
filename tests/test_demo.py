"""The demo, and the claim that its numbers are real.

The most important tests in this file are the ones that *change the world and
check the report changes with it*. A demo whose output is the same whether or not
the code works is a screenshot.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ap2_min.models import inr
from demo.batch import PLAN, REPORT_PATH, Report, main_async, measure, run_batch
from gateway.audit import Event
from gateway.bootstrap import Gateway, build_gateway
from gateway.db import MEMORY
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
    gateway = build_gateway(db_path=MEMORY, use_llm=False, sleep=lambda _seconds: None)
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


def _no_world_event(gateway: Gateway, index: int, goal: object) -> Callable[[], None] | None:
    """Stand-in for demo.batch._interleave_for with nothing scripted."""
    return None


@pytest.mark.asyncio
async def test_removing_the_stock_event_changes_attempt_six(demo_gateway: Gateway) -> None:
    """Without the concurrent buyer, attempt 6 is an ordinary purchase."""
    import demo.batch as batch

    original = batch._interleave_for
    batch._interleave_for = _no_world_event
    try:
        results, _ = await run_batch(demo_gateway, quiet=True)
    finally:
        batch._interleave_for = original

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
        gateway = build_gateway(db_path=MEMORY, use_llm=False, sleep=lambda _s: None)
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

    class NoNetwork(socket.socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("the demo opened a socket; it is supposed to be offline")

    monkeypatch.setattr(socket, "socket", NoNetwork)

    results, _ = await run_batch(demo_gateway, quiet=True)
    assert measure(demo_gateway, results).line() == EXPECTED_LINE


# ---------------------------------------------------------------------------
# Determinism at the CLI level, and immunity to leftover state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_the_cli_twice_produces_an_identical_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`make demo` twice must give the same answer. This is that, through main().

    Not the same thing as `test_the_batch_is_deterministic`, which builds two
    gateways by hand. This drives the actual entry point, writes the actual
    report.json, and compares the whole file — which is what a reviewer running
    the command twice will see.
    """
    first_exit = await main_async(["--json"])
    first = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    second_exit = await main_async(["--json"])
    second = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert first_exit == second_exit == 0
    assert first["line"] == second["line"] == EXPECTED_LINE
    assert first["captured_paise"] == second["captured_paise"] == inr(4096)
    assert first["audit_rows"] == second["audit_rows"]
    # Everything except the chain tip, which is a hash over randomised ECDSA
    # signatures and wall-clock timestamps and is *supposed* to differ.
    volatile = {"audit_chain_tip", "attempts_detail"}
    assert {k: v for k, v in first.items() if k not in volatile} == {
        k: v for k, v in second.items() if k not in volatile
    }
    assert first["audit_chain_tip"] != second["audit_chain_tip"], (
        "identical tips would mean the run was not actually re-executed"
    )


@pytest.mark.asyncio
async def test_the_demo_ignores_gateway_db_and_starts_from_an_empty_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A leftover database must not change the report. Regression test.

    Found in review: once .env was honoured, `GATEWAY_DB=run/gateway.db` made the
    demo persistent, so a second run inherited the first run's spend and the
    reconciliation check failed with "signed receipts total ₹4,096.00 but the
    spend ledger says ₹121,802.00". The demo now pins its own in-memory database
    whatever the environment says.
    """
    poisoned = tmp_path / "leftover.db"
    monkeypatch.setenv("GATEWAY_DB", str(poisoned))

    assert await main_async(["--json"]) == 0
    first = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert await main_async(["--json"]) == 0
    second = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert first["line"] == second["line"] == EXPECTED_LINE
    assert not poisoned.exists(), "the demo must not have written to $GATEWAY_DB at all"


@pytest.mark.asyncio
async def test_the_report_recomputes_from_the_audit_chain_alone(
    demo_gateway: Gateway,
) -> None:
    """Rebuild the headline numbers from the audit database, ignoring the results.

    `measure()` reads AttemptResult objects. This recomputes the same figures from
    nothing but persisted audit rows — a completely independent path — and
    requires them to agree. If the in-memory result objects and the durable record
    ever disagreed, one of them would be lying.
    """
    results, _ = await run_batch(demo_gateway, quiet=True)
    report = measure(demo_gateway, results)
    audit = demo_gateway.audit

    captured_rows = [
        row
        for row in audit.rows(event=Event.PAYMENT_RECEIPT_ISSUED)
        if row.payload["status"] == "captured"
    ]
    allow_rows = [
        row for row in audit.rows(event=Event.DECISION) if row.payload["outcome"] == "ALLOW"
    ]
    denied_rows = audit.rows(event="trusted_surface.gate_denied")
    recovered_rows = audit.rows(event=Event.RECOVERY_SUCCEEDED)
    plan_rows = audit.rows(event=Event.AGENT_PLAN)

    assert len(plan_rows) == report.attempts == 6
    assert len(captured_rows) == report.paid == 4
    assert len(denied_rows) == report.human_denied == 1
    assert len(recovered_rows) == report.recovered == 1
    assert sum(int(r.payload["amount"]) for r in captured_rows) == inr(4096)
    assert len(captured_rows) <= len(allow_rows)
    assert all(row.human_reason for row in audit.rows())
    assert audit.verify_chain().ok
