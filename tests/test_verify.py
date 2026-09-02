"""The deterministic verifier.

One test per check, plus the boundary cases that decide whether this is a real
gate or a suggestion. Read top to bottom, these are the complete list of
conditions under which this system will move money.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from ap2_min.builders import closed_payment_mandate, open_payment_mandate
from ap2_min.models import inr
from ap2_min.roles import ROLE_SHOPPING_AGENT, ROLE_USER
from gateway.ledger import InMemoryLedgerView
from gateway.mandates import KeyRing, Signer, checkout_hash, utcnow
from gateway.verify import Code, Outcome, verify_payment_mandate

from .conftest import make_signer
from .factories import MERCHANT_ID, Scenario

# ---------------------------------------------------------------------------
# ALLOW
# ---------------------------------------------------------------------------


def test_a_well_formed_purchase_within_every_bound_is_allowed(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    decision = verify_payment_mandate(
        scenario.present(), scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.ALLOW, decision.human_reason
    assert decision.allowed
    assert decision.amount == inr(1299)
    assert decision.payee == MERCHANT_ID
    assert decision.open_mandate_id == scenario.open_payment.mandate_id
    assert all(c.passed for c in decision.checks)


def test_an_allow_records_every_check_it_ran(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """Explainability is not a log line after the fact; it is the decision itself."""
    decision = verify_payment_mandate(
        scenario.present(), scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    names = [c.name for c in decision.checks]
    assert names == [
        "signature",
        "vct",
        "presenter_role",
        "not_expired",
        "vct",
        "not_expired",
        "key_binding",
        "currency",
        "allowed_payees",
        "amount_range",
        "budget",
        "execution_date",
        "checkout_reference",
        "nonce",
    ]


def test_the_budget_check_reports_the_arithmetic_it_did(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    ledger_view.add_spend(scenario.open_payment.mandate_id, inr(2000))
    decision = verify_payment_mandate(
        scenario.present(), scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    budget = next(c for c in decision.checks if c.name == "budget")
    assert budget.detail == {
        "already_spent": inr(2000),
        "requested": inr(1299),
        "projected": inr(3299),
        "budget": inr(5000),
        "remaining_after": inr(1701),
    }


def test_exactly_at_the_per_transaction_ceiling_is_allowed(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """The bound is inclusive. ₹1,500.00 on a ₹1,500 limit must pass."""
    scenario.amount = inr(1500)
    scenario.__post_init__()
    decision = verify_payment_mandate(
        scenario.present(), scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.ALLOW, decision.human_reason


def test_exactly_at_the_budget_is_allowed(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    ledger_view.add_spend(scenario.open_payment.mandate_id, inr(5000) - inr(1299))
    decision = verify_payment_mandate(
        scenario.present(), scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.ALLOW, decision.human_reason


def test_one_paise_over_the_budget_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """Off-by-one at a money boundary is the bug that matters."""
    ledger_view.add_spend(scenario.open_payment.mandate_id, inr(5000) - inr(1299) + 1)
    decision = verify_payment_mandate(
        scenario.present(), scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.BUDGET_EXCEEDED
    failure = next(c for c in decision.checks if not c.passed)
    assert failure.detail["over_by"] == 1


# ---------------------------------------------------------------------------
# DENY — the agent presented a closed mandate that violates a bound
# ---------------------------------------------------------------------------


def test_amount_over_the_per_transaction_ceiling_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """An agent that *forces* an over-limit closed mandate gets DENY, not a gate.

    Escalation is for an agent that admits it is short of authority. Forcing is
    not escalation.
    """
    scenario.amount = inr(4999)
    scenario.__post_init__()
    decision = verify_payment_mandate(
        scenario.present(), scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.AMOUNT_OUT_OF_RANGE
    assert decision.human_reason is not None
    assert "1,500.00" in decision.human_reason
    assert not decision.needs_human


def test_running_spend_over_budget_is_denied_with_a_reason_object_not_an_exception(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """Failure mode 4. A budget breach is an answer, not a crash."""
    ledger_view.add_spend(scenario.open_payment.mandate_id, inr(4800))
    decision = verify_payment_mandate(
        scenario.present(), scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.BUDGET_EXCEEDED
    body = decision.error_response()
    assert body["error"] == "denied"
    assert body["code"] == Code.BUDGET_EXCEEDED
    assert body["checks"][0]["detail"]["already_spent"] == inr(4800)
    assert body["checks"][0]["detail"]["over_by"] == inr(1099)


def test_a_payee_outside_the_allow_list_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    token = scenario.present(payee="m_definitely_not_a_merchant")
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.PAYEE_NOT_ALLOWED


def test_a_replayed_nonce_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    token = scenario.present(nonce="a" * 32)
    first = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert first.outcome is Outcome.ALLOW

    ledger_view.burn_nonce("a" * 32, "pm_first")
    # A *different* mandate reusing the burned nonce.
    replay = scenario.present(nonce="a" * 32, mandate_id="pm_second")
    second = verify_payment_mandate(
        replay, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert second.outcome is Outcome.DENY
    assert second.code == Code.REPLAYED_NONCE


def test_a_mandate_bound_to_a_different_checkout_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView, merchant_signer: Signer
) -> None:
    """Same amount, same payee, different cart. This is the substitution attack."""
    from ap2_min.builders import closed_checkout_mandate

    from .factories import cart

    other_checkout = merchant_signer.sign(
        closed_checkout_mandate(cart=cart(inr(1299), cart_id="cart_other", sku="SF-RUN-999")),
        ttl_seconds=900,
        now=scenario.now,
    )
    token = scenario.present(checkout_jws=other_checkout)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.REFERENCE_MISMATCH


def test_a_reference_pinned_open_mandate_rejects_a_different_checkout(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView, user_signer: Signer
) -> None:
    """``payment.reference`` pins a standing authorisation to one checkout."""
    pinned = open_payment_mandate(
        budget=inr(5000),
        amount_max=inr(1500),
        allowed_payees=[MERCHANT_ID],
        pinned_checkout_hash="f" * 64,
        cnf=scenario.agent.cnf,
    )
    pinned_jws = user_signer.sign(pinned, ttl_seconds=3600, now=scenario.now)
    token = scenario.present(open_jws=pinned_jws)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.REFERENCE_MISMATCH
    failure = next(c for c in decision.checks if not c.passed)
    assert failure.detail["pinned_to"] == "f" * 64


def test_an_expired_payment_mandate_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    token = scenario.present()
    decision = verify_payment_mandate(
        token,
        scenario.checkout_jws,
        ledger_view,
        keyring=keyring,
        now=scenario.now + timedelta(hours=2),
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.EXPIRED


def test_an_expired_standing_authorisation_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView, user_signer: Signer
) -> None:
    """A fresh closed mandate cannot resurrect a lapsed standing authorisation."""
    stale_open = user_signer.sign(
        scenario.open_payment, ttl_seconds=60, now=scenario.now - timedelta(days=2)
    )
    token = scenario.present(open_jws=stale_open)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.OPEN_MANDATE_INVALID
    failure = next(c for c in decision.checks if not c.passed)
    assert failure.detail["underlying_code"] == Code.EXPIRED


def test_an_execution_date_outside_the_authorised_window_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView, user_signer: Signer
) -> None:
    future_only = open_payment_mandate(
        budget=inr(5000),
        amount_max=inr(1500),
        allowed_payees=[MERCHANT_ID],
        not_before=scenario.now + timedelta(days=7),
        not_after=scenario.now + timedelta(days=14),
        cnf=scenario.agent.cnf,
    )
    token = scenario.present(
        open_jws=user_signer.sign(future_only, ttl_seconds=86400 * 30, now=scenario.now)
    )
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.EXECUTION_WINDOW


def test_an_execution_date_far_from_now_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """A mandate that says it executes tomorrow may not execute today."""
    token = scenario.present(execution_date=scenario.now + timedelta(days=1))
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.EXECUTION_WINDOW


# ---------------------------------------------------------------------------
# DENY — the envelope itself is bad. Failure mode 3.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected_code"),
    [
        ("", Code.MALFORMED),
        ("not.a.jwt", Code.MALFORMED),
        ("eyJhbGciOiJFUzI1NiJ9.e30", Code.MALFORMED),
    ],
)
def test_a_malformed_mandate_is_denied_at_the_boundary(
    token: str,
    expected_code: str,
    scenario: Scenario,
    keyring: KeyRing,
    ledger_view: InMemoryLedgerView,
) -> None:
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == expected_code
    assert decision.checks[0].name == "signature"


def test_a_forged_signature_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    token = scenario.present()
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    decision = verify_payment_mandate(
        tampered, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.BAD_SIGNATURE


def test_a_mandate_signed_by_an_unknown_key_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    stranger = make_signer("key_stranger", ROLE_SHOPPING_AGENT)
    token = scenario.present(signer=stranger)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.UNKNOWN_KEY


def test_a_merchant_may_not_present_the_mandate_authorising_its_own_payment(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView, merchant_signer: Signer
) -> None:
    token = scenario.present(signer=merchant_signer)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.WRONG_ISSUER


def test_a_standing_authorisation_not_signed_by_the_user_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView, agent_signer: Signer
) -> None:
    """The agent cannot write its own permission slip."""
    self_issued = agent_signer.sign(scenario.open_payment, ttl_seconds=3600, now=scenario.now)
    token = scenario.present(open_jws=self_issued)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.OPEN_MANDATE_INVALID
    failure = next(c for c in decision.checks if not c.passed)
    assert failure.detail["underlying_code"] == Code.WRONG_ISSUER


def test_a_stolen_standing_authorisation_cannot_be_presented_by_another_agent(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """Key binding. The open mandate names one holder key; only it may present.

    Without this, an open mandate that leaks anywhere is bearer authority.
    """
    other_agent = make_signer("key_agent_2", ROLE_SHOPPING_AGENT)
    keyring.register_signer(other_agent)
    token = scenario.present(signer=other_agent)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.NOT_KEY_BOUND


def test_an_unbound_standing_authorisation_fails_closed(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView, user_signer: Signer
) -> None:
    """No ``cnf`` means no holder. We refuse rather than treat it as bearer."""
    unbound = open_payment_mandate(
        budget=inr(5000), amount_max=inr(1500), allowed_payees=[MERCHANT_ID], cnf=None
    )
    token = scenario.present(open_jws=user_signer.sign(unbound, ttl_seconds=3600, now=scenario.now))
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.NOT_KEY_BOUND


def test_a_closed_mandate_embedding_another_closed_mandate_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """``open_mandate_jws`` must actually be an OPEN mandate."""
    inner_closed = scenario.present()
    token = scenario.present(open_jws=inner_closed)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    # The inner token is agent-signed, so it fails the user-role requirement first.
    assert decision.code == Code.OPEN_MANDATE_INVALID
    failure = next(c for c in decision.checks if not c.passed)
    assert failure.detail["underlying_code"] == Code.WRONG_ISSUER


def test_a_user_signed_closed_mandate_in_the_open_slot_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView, user_signer: Signer
) -> None:
    """Right signer, wrong vct. Both must hold."""
    closed_but_user_signed = user_signer.sign(
        closed_payment_mandate(
            payee=MERCHANT_ID,
            payee_name="StrideFit Sportswear",
            amount=inr(100),
            payment_instrument="upi",
            checkout_hash=checkout_hash(scenario.checkout_jws),
            open_mandate_jws=scenario.open_payment_jws,
            execution_date=scenario.now,
        ),
        ttl_seconds=600,
        now=scenario.now,
    )
    token = scenario.present(open_jws=closed_but_user_signed)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.WRONG_VCT


def test_a_currency_mismatch_is_denied(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """Comparing 1500 USD against a 1500 INR limit would silently pass."""
    contents = scenario.closed_payment()
    usd = contents.model_copy(update={"currency": "USD"})
    token = scenario.agent.sign(usd, ttl_seconds=600, now=scenario.now)
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.DENY
    assert decision.code == Code.CURRENCY_MISMATCH


# ---------------------------------------------------------------------------
# UNRESOLVED_CONSTRAINT — the gate. Failure mode 8.
# ---------------------------------------------------------------------------


def test_presenting_only_a_standing_authorisation_is_an_unresolved_constraint(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    decision = verify_payment_mandate(
        scenario.present_open(),
        scenario.checkout_jws,
        ledger_view,
        keyring=keyring,
        now=scenario.now,
    )
    assert decision.outcome is Outcome.UNRESOLVED_CONSTRAINT
    assert decision.needs_human
    assert decision.code == Code.NEEDS_CLOSED_MANDATE
    body = decision.error_response()
    assert body["error"] == "unresolved_constraint"
    assert body["constraint"] == Code.NEEDS_CLOSED_MANDATE


def test_an_over_limit_escalation_names_the_constraint_and_the_gap(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """The user must see a specific question, not a generic consent prompt.

    A gate a human cannot understand is a gate a human clicks through.
    """
    decision = verify_payment_mandate(
        scenario.present_open(),
        scenario.checkout_jws,
        ledger_view,
        keyring=keyring,
        now=scenario.now,
        checkout_total=inr(4999),
    )
    assert decision.outcome is Outcome.UNRESOLVED_CONSTRAINT
    assert decision.code == Code.ABOVE_STANDING_LIMIT
    assert decision.human_reason is not None
    assert "4,999.00" in decision.human_reason
    assert "1,500.00" in decision.human_reason
    failure = decision.checks[-1]
    assert failure.detail == {
        "open_mandate_id": scenario.open_payment.mandate_id,
        "amount": inr(4999),
        "limit": inr(1500),
    }


def test_an_over_budget_escalation_names_the_budget(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    ledger_view.add_spend(scenario.open_payment.mandate_id, inr(4500))
    decision = verify_payment_mandate(
        scenario.present_open(),
        scenario.checkout_jws,
        ledger_view,
        keyring=keyring,
        now=scenario.now,
        checkout_total=inr(1000),
    )
    assert decision.outcome is Outcome.UNRESOLVED_CONSTRAINT
    assert decision.code == Code.ABOVE_STANDING_LIMIT
    assert decision.human_reason is not None
    assert "5,000.00" in decision.human_reason


def test_an_in_scope_escalation_still_asks_for_a_closed_mandate(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """Within limits, but still only a standing authorisation: consent is missing."""
    decision = verify_payment_mandate(
        scenario.present_open(),
        scenario.checkout_jws,
        ledger_view,
        keyring=keyring,
        now=scenario.now,
        checkout_total=inr(500),
    )
    assert decision.outcome is Outcome.UNRESOLVED_CONSTRAINT
    assert decision.code == Code.NEEDS_CLOSED_MANDATE


# ---------------------------------------------------------------------------
# Determinism and purity — the properties the whole design rests on
# ---------------------------------------------------------------------------


def test_the_verifier_is_deterministic(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    token = scenario.present()
    runs = [
        verify_payment_mandate(
            token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
        ).as_dict()
        for _ in range(25)
    ]
    assert all(run == runs[0] for run in runs)


def test_the_verifier_does_not_mutate_the_ledger(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """Deciding whether a spend is allowed must not itself record a spend."""
    before = ledger_view.spent_under(scenario.open_payment.mandate_id)
    token = scenario.present()
    for _ in range(5):
        assert (
            verify_payment_mandate(
                token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
            ).outcome
            is Outcome.ALLOW
        )
    assert ledger_view.spent_under(scenario.open_payment.mandate_id) == before
    assert not ledger_view.nonce_seen(scenario.closed_payment().nonce)


def test_the_verifier_never_raises_on_hostile_input(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """Every rejection is a Decision. Callers on the money path write no try blocks."""
    hostile = [
        "",
        "....",
        "a" * 5000,
        "eyJhbGciOiJub25lIn0.eyJ2Y3QiOiJtYW5kYXRlLnBheW1lbnQuMSJ9.",
        scenario.present()[:-5],
        scenario.present().replace(".", "", 1),
    ]
    for token in hostile:
        decision = verify_payment_mandate(
            token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
        )
        assert decision.outcome is Outcome.DENY


def test_every_denial_carries_a_human_reason(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """ "Explainable" means every path out of here can be read aloud to a person."""
    ledger_view.add_spend(scenario.open_payment.mandate_id, inr(4900))
    cases = [
        scenario.present(payee="m_nope"),
        scenario.present(),
        "garbage",
    ]
    for token in cases:
        decision = verify_payment_mandate(
            token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
        )
        assert decision.outcome is not Outcome.ALLOW
        assert decision.human_reason
        assert len(decision.human_reason) > 20


def test_the_first_failing_check_is_the_reported_one(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView
) -> None:
    """Short-circuit order is stable, so the reason is reproducible."""
    ledger_view.add_spend(scenario.open_payment.mandate_id, inr(4900))
    token = scenario.present(payee="m_nope")  # both payee and budget would fail
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.code == Code.PAYEE_NOT_ALLOWED  # payee is checked first
    assert sum(1 for c in decision.checks if not c.passed) == 1


# ---------------------------------------------------------------------------
# Absent constraints
# ---------------------------------------------------------------------------


def test_an_absent_constraint_does_not_fail(
    scenario: Scenario, keyring: KeyRing, ledger_view: InMemoryLedgerView, user_signer: Signer
) -> None:
    """A constraint that was never set has not been violated.

    Reported honestly as ``constrained: False`` rather than silently passing, so
    an auditor can see which bounds the user actually applied.
    """
    minimal = open_payment_mandate(amount_max=inr(1500), cnf=scenario.agent.cnf)
    token = scenario.present(open_jws=user_signer.sign(minimal, ttl_seconds=3600, now=scenario.now))
    decision = verify_payment_mandate(
        token, scenario.checkout_jws, ledger_view, keyring=keyring, now=scenario.now
    )
    assert decision.outcome is Outcome.ALLOW
    by_name = {c.name: c for c in decision.checks}
    assert by_name["budget"].detail == {"constrained": False}
    assert by_name["allowed_payees"].detail == {"constrained": False}


def test_role_constants_are_what_the_keyring_stores(keyring: KeyRing) -> None:
    assert keyring.get("key_user_1").role == ROLE_USER
    assert keyring.get("key_agent_1").role == ROLE_SHOPPING_AGENT


def test_utcnow_is_timezone_aware() -> None:
    assert utcnow().tzinfo is not None
