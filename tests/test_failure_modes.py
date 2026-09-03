"""The eight failure modes, each asserted on both its outcome and its audit row.

Track 01 asks for at least one failure handled gracefully. These are eight, and
every one is checked twice: that the system did the right thing, and that the
audit trail says so. A recovery nobody can prove happened is not a recovery you
would want to explain to a regulator.

The failure modes, and where each is defended:

    1  bank decline          gateway/recovery.py     instrument fallback, bounded
    2  API drop / timeout    gateway/recovery.py     circuit breaker, mandate unspent
    3  invalid mandate       gateway/mandates.py     typed rejection at the boundary
    4  budget breach         gateway/verify.py       DENY with a reason object
    5  stock race            merchant/checkout.py    re-check before every attempt
    6  duplicate submit      gateway/payments.py     sha256 idempotency + lease
    7  hallucinated SKU      merchant/service.py     flat not_found, nothing signed
    8  out-of-band request   gateway/trusted_surface.py  unresolved_constraint → human
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import Client

from ap2_min.builders import closed_payment_mandate
from ap2_min.models import inr
from gateway.audit import Event
from gateway.bootstrap import Gateway
from gateway.mandates import utcnow
from gateway.razorpay_client import METHOD_UPI, FakeRail
from merchant.mcp_server import build_server
from shopping_agent.agent import (
    STATUS_HUMAN_DENIED,
    STATUS_PAID,
    Goal,
    ShoppingAgent,
)
from shopping_agent.human import SimulatedShopper, always_approve, always_deny
from shopping_agent.mcp_tools import McpMerchantTools

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The three modules that decide whether money moves. Nothing in them may reach a
#: language model, and this list is what the grep test enforces.
MONEY_PATH_MODULES = ["gateway/verify.py", "gateway/payments.py", "gateway/recovery.py"]


# ---------------------------------------------------------------------------
# Helpers: drive a purchase to a chosen point
# ---------------------------------------------------------------------------


def confirmed_checkout(wired: Gateway, sku: str = "SF-RUN-001") -> dict[str, Any]:
    """Cart → merchant-signed checkout → buyer's authorisation accepted."""
    merchant = wired.merchant
    cart = merchant.assemble_cart([{"sku": sku, "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    confirmed = merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)
    assert confirmed["status"] == "confirmed", confirmed
    return {**checkout, "cart": cart}


def signed_payment(wired: Gateway, checkout: dict[str, Any], **overrides: Any) -> str:
    cart = checkout["cart"]
    now = utcnow()
    contents = closed_payment_mandate(
        payee=overrides.pop("payee", cart["merchant_id"]),
        payee_name=cart["merchant_name"],
        amount=overrides.pop("amount", cart["total"]),
        payment_instrument=overrides.pop("instrument", METHOD_UPI),
        checkout_hash=overrides.pop("checkout_hash", checkout["checkout_hash"]),
        open_mandate_jws=overrides.pop("open_jws", wired.open_payment_jws),
        execution_date=now,
        **overrides,
    )
    return wired.agent.sign(contents, ttl_seconds=600, now=now)


@pytest_asyncio.fixture
async def agent_and_rail(wired: Gateway):  # type: ignore[no-untyped-def]
    """A live agent over MCP, plus the rail so a test can misbehave it."""
    server, _ = build_server(wired)
    async with Client(server) as client:
        shopper = SimulatedShopper(wired.trusted_surface, policy=always_deny)
        agent = ShoppingAgent(
            tools=McpMerchantTools(client),
            signer=wired.agent,
            open_checkout_jws=wired.open_checkout_jws,
            open_payment_jws=wired.open_payment_jws,
            open_payment=wired.open_payment_contents,
            audit=wired.audit,
            human=shopper.gate_view(),
        )
        yield agent, wired.rail, shopper


# ---------------------------------------------------------------------------
# 1. Bank decline
# ---------------------------------------------------------------------------


def test_failure_1_bank_decline_falls_back_and_recovers(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    checkout = confirmed_checkout(wired)
    fake_rail.decline(methods={METHOD_UPI}, times=1)

    response = wired.merchant.initiate_payment(
        checkout["checkout_id"], signed_payment(wired, checkout)
    )

    assert response["status"] == "captured"
    assert response["recovered"] is True
    assert response["methods_tried"] == [METHOD_UPI, "payment_link"]
    assert fake_rail.captured_total() == inr(1299), "recovered, charged once"

    assert wired.audit.rows(event=Event.PAYMENT_DECLINED)
    assert wired.audit.rows(event=Event.RECOVERY_METHOD_FALLBACK)
    assert wired.audit.rows(event=Event.RECOVERY_SUCCEEDED)


def test_failure_1_persistent_decline_stops_at_three_with_a_signed_receipt(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    checkout = confirmed_checkout(wired)
    fake_rail.decline(times=None)

    response = wired.merchant.initiate_payment(
        checkout["checkout_id"], signed_payment(wired, checkout)
    )

    assert response["status"] == "failed"
    assert response["payment_receipt"]["failure_code"] == "recovery.attempts_exhausted"
    assert response["attempts"] == 3
    assert response["payment_receipt_jws"], "a failure gets a signed receipt too"
    assert fake_rail.captured_total() == 0

    exhausted = wired.audit.rows(event=Event.RECOVERY_EXHAUSTED)
    assert len(exhausted) == 1
    assert exhausted[0].payload["max_attempts"] == 3


# ---------------------------------------------------------------------------
# 2. API drop / timeout
# ---------------------------------------------------------------------------


def test_failure_2_a_rail_timeout_opens_the_breaker_and_leaves_the_mandate_unspent(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    from gateway.payments import idempotency_key

    checkout = confirmed_checkout(wired)
    mandate_jws = signed_payment(wired, checkout)
    fake_rail.timeout(times=None)

    response = wired.merchant.initiate_payment(checkout["checkout_id"], mandate_jws)

    assert response["status"] == "deferred"
    assert response["mandate_spent"] is False
    assert "payment_receipt" not in response, "a deferral issues no receipt on purpose"
    assert fake_rail.captured_total() == 0

    assert wired.audit.rows(event=Event.RAIL_TIMEOUT)
    assert wired.audit.rows(event=Event.CIRCUIT_OPENED)
    deferred = wired.audit.rows(event=Event.CIRCUIT_DEFERRED)
    assert len(deferred) == 1
    assert deferred[0].payload["mandate_spent"] is False

    from gateway.mandates import load_payment_mandate

    contents, _ = load_payment_mandate(mandate_jws, wired.keyring)
    record = wired.ledger.get_idempotency(idempotency_key(contents.mandate_id))
    assert record is not None
    assert record.status == "in_flight", "not terminal, so still presentable"


def test_failure_2_the_same_mandate_succeeds_on_the_next_tick(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """ "Retry next tick" is the point of deferring rather than failing."""
    checkout = confirmed_checkout(wired)
    mandate_jws = signed_payment(wired, checkout)

    fake_rail.timeout(times=None)
    assert wired.merchant.initiate_payment(checkout["checkout_id"], mandate_jws)["status"] == (
        "deferred"
    )

    fake_rail.reset_rules()
    wired.breaker.record_success()  # the operator's health check closed it

    response = wired.merchant.initiate_payment(checkout["checkout_id"], mandate_jws)
    assert response["status"] == "captured"
    assert fake_rail.captured_total() == inr(1299), "one charge across both ticks"


def test_failure_2_a_timeout_that_actually_captured_is_not_charged_twice(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """The worst case: we did not hear back, but the money moved."""
    from gateway.payments import idempotency_key

    checkout = confirmed_checkout(wired)
    mandate_jws = signed_payment(wired, checkout)
    fake_rail.timeout(times=None)
    wired.merchant.initiate_payment(checkout["checkout_id"], mandate_jws)

    from gateway.mandates import load_payment_mandate

    contents, _ = load_payment_mandate(mandate_jws, wired.keyring)
    record = wired.ledger.get_idempotency(idempotency_key(contents.mandate_id))
    assert record is not None
    fake_rail.reset_rules()
    fake_rail.complete_test_payment(record.order_ids[0], method=METHOD_UPI)  # it landed
    wired.breaker.record_success()

    response = wired.merchant.initiate_payment(checkout["checkout_id"], mandate_jws)
    assert response["status"] == "captured"
    assert response["payment_receipt"]["order_id"] == record.order_ids[0]
    assert fake_rail.captured_total() == inr(1299)
    assert wired.audit.rows(event=Event.RECOVERY_ABORTED_PRIOR_CAPTURE)


# ---------------------------------------------------------------------------
# 3. Invalid mandate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mandate", "expected_code"),
    [
        ("", "mandate.malformed"),
        ("not-a-jwt", "mandate.malformed"),
        ("a.b.c", "mandate.malformed"),
        ("eyJhbGciOiJub25lIn0.eyJ2Y3QiOiJ4In0.", "mandate.malformed"),
    ],
)
def test_failure_3_a_malformed_mandate_is_typed_and_never_reaches_the_rail(
    wired: Gateway, fake_rail: FakeRail, mandate: str, expected_code: str
) -> None:
    checkout = confirmed_checkout(wired)
    response = wired.merchant.initiate_payment(checkout["checkout_id"], mandate)

    assert response["error"] == expected_code
    assert fake_rail.calls == [], "nothing reached Razorpay"
    assert wired.audit.rows(event=Event.MANDATE_REJECTED)


def test_failure_3_a_forged_signature_is_rejected_at_the_boundary(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    checkout = confirmed_checkout(wired)
    jws = signed_payment(wired, checkout)
    header, payload, signature = jws.split(".")
    tampered = f"{header}.{payload}.{('A' if signature[0] != 'A' else 'B')}{signature[1:]}"

    response = wired.merchant.initiate_payment(checkout["checkout_id"], tampered)

    assert response["error"] == "mandate.bad_signature"
    assert fake_rail.calls == []
    rejected = wired.audit.rows(event=Event.MANDATE_REJECTED)
    assert rejected[-1].payload["code"] == "mandate.bad_signature"


def test_failure_3_a_mandate_missing_a_required_field_cannot_even_be_built() -> None:
    """The model refuses it before a signature is possible."""
    with pytest.raises(ValueError, match="missing required fields"):
        from ap2_min.models import PaymentMandateContents
        from ap2_min.vct import VCT_PAYMENT_CLOSED

        PaymentMandateContents(vct=VCT_PAYMENT_CLOSED, mandate_id="pm_x", nonce="n" * 16)


def test_failure_3_a_mandate_from_an_unknown_key_is_rejected(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    from ap2_min.roles import ROLE_SHOPPING_AGENT
    from gateway.mandates import Signer, generate_keypair

    key, _ = generate_keypair()
    stranger = Signer(kid="key_stranger", role=ROLE_SHOPPING_AGENT, private_key=key)

    checkout = confirmed_checkout(wired)
    cart = checkout["cart"]
    now = utcnow()
    contents = closed_payment_mandate(
        payee=cart["merchant_id"],
        payee_name=cart["merchant_name"],
        amount=cart["total"],
        payment_instrument=METHOD_UPI,
        checkout_hash=checkout["checkout_hash"],
        open_mandate_jws=wired.open_payment_jws,
        execution_date=now,
    )
    response = wired.merchant.initiate_payment(
        checkout["checkout_id"], stranger.sign(contents, ttl_seconds=600, now=now)
    )
    assert response["error"] == "mandate.unknown_key"
    assert fake_rail.calls == []


# ---------------------------------------------------------------------------
# 4. Budget breach
# ---------------------------------------------------------------------------


def test_failure_4_a_budget_breach_is_a_reason_object_not_an_exception(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """The agent forced a closed mandate it could not have known was over budget."""
    wired.ledger.record_spend(
        open_mandate_id=wired.open_payment_contents.mandate_id,
        payment_mandate_id="pm_earlier_today",
        amount=inr(4800),
        currency="INR",
        payee="m_stridefit",
    )
    checkout = confirmed_checkout(wired)

    response = wired.merchant.initiate_payment(
        checkout["checkout_id"], signed_payment(wired, checkout)
    )

    assert response["error"] == "denied"
    assert response["code"] == "payment.budget.exceeded"
    assert response["charged"] is False
    failure = response["checks"][0]
    assert failure["detail"]["already_spent"] == inr(4800)
    assert failure["detail"]["over_by"] == inr(1099)
    assert fake_rail.calls == []

    decision = wired.audit.rows(event=Event.DECISION)[-1]
    assert decision.payload["outcome"] == "DENY"
    assert decision.payload["code"] == "payment.budget.exceeded"
    assert "5,000.00" in (decision.human_reason or "")


def test_failure_4_an_over_cap_closed_mandate_is_denied_not_gated(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """Forcing is not escalating. An agent that pushes gets a refusal."""
    checkout = confirmed_checkout(wired, sku="SF-RUN-001")
    forced = signed_payment(wired, checkout, amount=inr(4999))

    response = wired.merchant.initiate_payment(checkout["checkout_id"], forced)

    assert response["error"] == "denied"
    assert response["code"] == "payment.amount_range.violated"
    assert fake_rail.calls == []


# ---------------------------------------------------------------------------
# 5. Stock race
# ---------------------------------------------------------------------------


def test_failure_5_stock_selling_out_between_checkout_and_payment_declines_cleanly(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    checkout = confirmed_checkout(wired)
    mandate = signed_payment(wired, checkout)

    wired.catalog.set_stock("SF-RUN-001", 0)  # another buyer, mid-flight

    response = wired.merchant.initiate_payment(checkout["checkout_id"], mandate)

    assert response["error"] == "stock.unavailable"
    assert response["charged"] is False
    assert "down to 0 in stock" in response["message"]
    assert fake_rail.calls == [], "the rail was never contacted"
    assert wired.ledger.total_captured() == 0
    assert wired.audit.rows(event=Event.STOCK_RECHECK_FAILED)


def test_failure_5_a_price_change_between_checkout_and_payment_declines_cleanly(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """The other half of the race: still in stock, no longer that price."""
    from dataclasses import replace

    checkout = confirmed_checkout(wired)
    mandate = signed_payment(wired, checkout)
    wired.catalog.products["SF-RUN-001"] = replace(wired.catalog.get("SF-RUN-001"), price=inr(1999))

    response = wired.merchant.initiate_payment(checkout["checkout_id"], mandate)

    assert response["error"] == "stock.unavailable"
    assert "1,999.00" in response["message"]
    assert fake_rail.calls == []


def test_failure_5_stock_selling_out_mid_recovery_stops_the_retries(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """The second window: the first attempt declined, and *then* it sold out."""
    checkout = confirmed_checkout(wired)
    mandate = signed_payment(wired, checkout)
    fake_rail.decline(methods={METHOD_UPI}, times=1)

    original = wired.store.recheck
    calls = {"n": 0}

    def recheck_then_sell_out(cart: Any) -> tuple[bool, str]:
        # Call 1 is initiate_payment's pre-verifier check; call 2 is the playbook
        # before its first attempt. The shelf empties after that, so the first
        # attempt runs and the retry is stopped.
        calls["n"] += 1
        if calls["n"] > 2:
            wired.catalog.set_stock("SF-RUN-001", 0)
        return original(cart)

    wired.store.recheck = recheck_then_sell_out  # type: ignore[method-assign]

    response = wired.merchant.initiate_payment(checkout["checkout_id"], mandate)

    assert response["status"] == "failed"
    assert response["payment_receipt"]["failure_code"] == "stock.unavailable"
    assert fake_rail.captured_total() == 0
    # One order for the attempt that ran before the shelf emptied, and none after.
    assert len(fake_rail.orders()) == 1
    assert wired.audit.rows(event=Event.STOCK_RECHECK_FAILED)


# ---------------------------------------------------------------------------
# 6. Duplicate submit
# ---------------------------------------------------------------------------


def test_failure_6_the_same_mandate_twice_returns_the_first_receipt(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    checkout = confirmed_checkout(wired)
    mandate = signed_payment(wired, checkout)

    first = wired.merchant.initiate_payment(checkout["checkout_id"], mandate)
    second = wired.merchant.initiate_payment(checkout["checkout_id"], mandate)

    assert first["status"] == second["status"] == "captured"
    assert second["replayed"] is True
    assert second["payment_receipt"]["receipt_id"] == first["payment_receipt"]["receipt_id"]
    assert second["payment_receipt_jws"] == first["payment_receipt_jws"]

    assert fake_rail.captured_total() == inr(1299), "exactly one charge"
    assert len(fake_rail.orders()) == 1, "exactly one order"
    assert wired.ledger.total_captured() == inr(1299)


def test_failure_6_five_submissions_still_charge_once(wired: Gateway, fake_rail: FakeRail) -> None:
    checkout = confirmed_checkout(wired)
    mandate = signed_payment(wired, checkout)
    responses = [
        wired.merchant.initiate_payment(checkout["checkout_id"], mandate) for _ in range(5)
    ]
    assert len({r["payment_receipt"]["receipt_id"] for r in responses}) == 1
    assert fake_rail.captured_total() == inr(1299)
    assert len(fake_rail.orders()) == 1


def test_failure_6_a_duplicate_submit_is_audited_as_such(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """Idempotency is invisible unless it is recorded, and an operator wants to
    know a client is retrying."""
    checkout = confirmed_checkout(wired)
    mandate = signed_payment(wired, checkout)
    wired.merchant.initiate_payment(checkout["checkout_id"], mandate)
    wired.merchant.initiate_payment(checkout["checkout_id"], mandate)

    received = wired.audit.rows(event=Event.PAYMENT_MANDATE_RECEIVED)
    assert len(received) == 2, "both submissions were seen"
    assert len(wired.audit.rows(event=Event.PAYMENT_CAPTURED)) == 1, "one capture"
    assert len(wired.audit.rows(event=Event.ORDER_CREATED)) == 1, "one order"


# ---------------------------------------------------------------------------
# 7. Hallucinated SKU
# ---------------------------------------------------------------------------


def test_failure_7_a_nonexistent_sku_is_a_flat_not_found(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    response = wired.merchant.check_product("SF-RUN-999")

    assert response["error"] == "product.not_found"
    assert response["sku"] == "SF-RUN-999"
    assert fake_rail.calls == []
    assert wired.audit.count(Event.CART_ASSEMBLED) == 0, "no cart was built"
    assert wired.audit.count(Event.CHECK_RESULT) == 0, "the verifier never ran"

    found = wired.audit.find(Event.PRODUCT_NOT_FOUND, sku="SF-RUN-999")
    assert len(found) == 1
    assert "does not exist" in (found[0].human_reason or "")


def test_failure_7_a_cart_containing_a_hallucinated_sku_is_refused(wired: Gateway) -> None:
    response = wired.merchant.assemble_cart([{"sku": "TOTALLY-MADE-UP", "qty": 1}])
    assert response["error"] == "product.not_found"


@pytest.mark.asyncio
async def test_failure_7_the_agent_replans_and_completes_the_purchase(
    agent_and_rail: Any, wired: Gateway
) -> None:
    """The agent is told about a product that does not exist, and recovers."""
    agent, _rail, _shopper = agent_and_rail
    result = await agent.attempt(
        Goal(
            label="running shoes under ₹1,500",
            query="running",
            filters={"category": "running_shoes", "max_price_inr": 1500},
            prefer_sku="SF-RUN-001",
            try_sku_first="SF-RUN-999",
        )
    )
    assert result.status == STATUS_PAID
    assert result.replans == 1
    assert result.sku == "SF-RUN-001"

    replanned = wired.audit.rows(event=Event.AGENT_REPLANNED)
    assert len(replanned) == 1
    assert replanned[0].payload["missing_sku"] == "SF-RUN-999"


# ---------------------------------------------------------------------------
# 8. Out-of-band request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_8_an_out_of_scope_purchase_is_escalated_and_can_be_denied(
    agent_and_rail: Any, wired: Gateway
) -> None:
    agent, rail, _shopper = agent_and_rail  # the shopper declines by default

    result = await agent.attempt(
        Goal(
            label="the carbon-plate marathon racing shoe",
            query="marathon",
            filters={"category": "running_shoes"},
            prefer_sku="SF-RUN-004",
        )
    )

    assert result.status == STATUS_HUMAN_DENIED
    assert result.escalated
    assert result.decision_code == "checkout.amount_exceeds_standing_limit"
    assert result.charged_amount == 0
    assert rail.captured_total() == 0

    assert wired.audit.rows(event=Event.CHECKOUT_UNRESOLVED)
    assert wired.audit.rows(event=Event.GATE_REQUESTED)
    assert wired.audit.rows(event=Event.GATE_DENIED)
    assert wired.audit.rows(event=Event.AGENT_ESCALATED)
    assert not wired.audit.rows(event=Event.PAYMENT_CAPTURED)


@pytest.mark.asyncio
async def test_failure_8_an_approved_escalation_completes_on_a_user_signed_mandate(
    agent_and_rail: Any, wired: Gateway
) -> None:
    agent, rail, shopper = agent_and_rail
    shopper.policy = always_approve

    result = await agent.attempt(
        Goal(
            label="the carbon-plate marathon racing shoe",
            query="marathon",
            filters={"category": "running_shoes"},
            prefer_sku="SF-RUN-004",
        )
    )

    assert result.status == STATUS_PAID
    assert result.escalated
    assert result.charged_amount == inr(4999)
    assert rail.captured_total() == inr(4999)

    assert wired.audit.rows(event=Event.GATE_APPROVED)
    approved = wired.audit.rows(event=Event.GATE_APPROVED)[0]
    assert approved.payload["scope"]["amount_range"] == [inr(4999), inr(4999)]
    assert approved.payload["scope"]["allowed_payees"] == ["m_stridefit"]
    assert "standing limit is unchanged" in (approved.human_reason or "")


@pytest.mark.asyncio
async def test_failure_8_the_agent_cannot_approve_on_its_own_behalf(
    agent_and_rail: Any,
) -> None:
    """Three separate reasons the agent cannot approve its own payment."""
    agent, _rail, _shopper = agent_and_rail

    # 1. The handle it was given exposes one read-only method and nothing else.
    from shopping_agent.human import GateView

    assert isinstance(agent.human, GateView)
    assert [n for n in dir(agent.human) if not n.startswith("_")] == ["await_decision"]
    assert not hasattr(agent, "trusted_surface")

    # 2. It holds a shopping-agent key. Only the buyer's key can sign an open
    #    mandate, and only the Trusted Surface holds that.
    assert agent.signer.role == "shopping_agent"

    # 3. The merchant offers it no tool that approves anything — see
    #    tests/test_mcp_tools.py::test_there_is_no_tool_that_grants_authority.


# ---------------------------------------------------------------------------
# The rule the whole design rests on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", MONEY_PATH_MODULES)
def test_no_language_model_on_the_money_path(module: str) -> None:
    """grep, as a test.

    The claim is that mandate verification, amount checks, fund release and
    idempotency are decided in deterministic code. A comment saying so is worth
    nothing; this fails the build if anyone imports a model into those modules.
    """
    result = subprocess.run(
        ["grep", "-nE", r"anthropic|from llm|import llm|llm\.", module],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, (
        f"{module} is on the money path and must not reference a language model:\n{result.stdout}"
    )
    assert result.stdout == ""


def test_the_money_path_modules_do_not_transitively_import_llm() -> None:
    """A direct grep is necessary but not sufficient — check the import graph too."""
    import ast

    for module in MONEY_PATH_MODULES:
        tree = ast.parse((REPO_ROOT / module).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "llm" not in imported, f"{module} imports llm"
        assert "anthropic" not in imported, f"{module} imports anthropic"


def test_the_policy_file_agrees_with_the_code(wired: Gateway) -> None:
    """config/policy.yaml lists where a model may not run. Keep it honest."""
    forbidden = set(wired.policy.llm.forbidden)
    assert {"mandate_verification", "amount_checks", "fund_release", "idempotency"} <= forbidden
    assert set(wired.policy.llm.allowed) == {"audit_narration", "product_selection"}


def test_narration_failure_does_not_stop_a_payment(wired: Gateway, fake_rail: FakeRail) -> None:
    """If the model is down, the money still moves and the audit row still writes."""
    from llm.client import FakeLLM
    from llm.reason import ReasonWriter

    broken = ReasonWriter(client=FakeLLM(fail=True), enabled=True)
    wired.merchant.reasons = broken

    checkout = confirmed_checkout(wired)
    response = wired.merchant.initiate_payment(
        checkout["checkout_id"], signed_payment(wired, checkout)
    )

    assert response["status"] == "captured"
    assert broken.stats["fallback"] > 0, "the model was tried and failed"
    for row in wired.audit.rows():
        assert row.human_reason, f"{row.event} lost its explanation when the model failed"
    assert wired.audit.verify_chain().ok


# ---------------------------------------------------------------------------
# Every failure mode leaves a readable trail
# ---------------------------------------------------------------------------


def test_every_audit_row_written_during_a_failure_is_explained(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    checkout = confirmed_checkout(wired)
    fake_rail.decline(times=None)
    wired.merchant.initiate_payment(checkout["checkout_id"], signed_payment(wired, checkout))

    rows = wired.audit.rows()
    assert len(rows) > 20
    for row in rows:
        assert row.human_reason, f"{row.event} has no human_reason"
    assert wired.audit.verify_chain().ok


def test_the_chain_survives_every_failure_mode_in_one_run(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """Run several failures back to back; the chain must still verify."""
    wired.merchant.check_product("NOPE")  # 7
    first = confirmed_checkout(wired)
    wired.merchant.initiate_payment(first["checkout_id"], "garbage")  # 3
    fake_rail.decline(methods={METHOD_UPI}, times=1)
    wired.merchant.initiate_payment(first["checkout_id"], signed_payment(wired, first))  # 1
    second = confirmed_checkout(wired, "SF-APP-001")
    wired.catalog.set_stock("SF-APP-001", 0)
    wired.merchant.initiate_payment(second["checkout_id"], signed_payment(wired, second))  # 5

    chain = wired.audit.verify_chain()
    assert chain.ok, chain.reason
    assert chain.rows_checked > 30
    assert fake_rail.captured_total() == inr(1299)
