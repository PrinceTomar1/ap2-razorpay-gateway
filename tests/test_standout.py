"""The layers added on top of the core: red team, benchmark, interop, timeline.

Each is a gate rather than a document, so each needs a test that would fail if
the gate stopped being real.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ap2_min.models import inr
from gateway.bootstrap import Gateway
from gateway.razorpay_client import FakeRail

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Red team
# ---------------------------------------------------------------------------


def test_every_red_team_attack_is_blocked() -> None:
    """The headline claim, re-run inside the suite rather than trusted from a file."""
    from redteam.attacks import run_all

    results = run_all()
    breached = [r for r in results if not r.blocked]
    assert breached == [], f"attacks succeeded: {[(r.name, r.error or r.code) for r in breached]}"
    assert len(results) >= 20, "the suite should be substantial, not a token gesture"


def test_no_red_team_attack_crashed_the_gateway() -> None:
    """A crash is a finding too — an unhandled exception on the money path."""
    from redteam.attacks import run_all

    crashed = [r for r in run_all() if r.error]
    assert crashed == [], f"attacks crashed rather than being refused: {crashed}"


def test_the_red_team_covers_every_defence_layer() -> None:
    """Defence in depth is only meaningful if each layer is actually exercised."""
    from redteam.attacks import run_all

    layers = {r.refused_by.split(" (")[0] for r in run_all()}
    for expected in (
        "gateway/mandates.py",
        "gateway/verify.py",
        "gateway/payments.py",
        "gateway/webhooks.py",
        "ap2_min/models.py",
    ):
        assert any(expected in layer for layer in layers), f"nothing exercised {expected}"


def test_the_red_team_would_report_a_breach(monkeypatch: pytest.MonkeyPatch) -> None:
    """A report that cannot come back red proves nothing.

    Forge an Attempt that charged money and confirm it is classified as a breach
    and rendered under the warning heading.
    """
    from redteam.attacks import Attempt
    from redteam.run import render

    breach = Attempt("planted", "Test", "charge without authority", "—", charged=inr(500), orders=1)
    assert not breach.blocked
    report = render([breach])
    assert "⚠ Breaches" in report
    assert "SEE BREACHES" in report


def test_each_attack_carries_a_goal_and_a_rationale() -> None:
    """An attack nobody can explain is an attack nobody will maintain."""
    from redteam.attacks import run_all

    for r in run_all():
        assert len(r.goal) > 20, f"{r.name} has no stated goal"
        assert len(r.rationale) > 20, f"{r.name} has no stated rationale"


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


def test_the_benchmark_has_zero_false_accepts() -> None:
    """The only benchmark number that would make the project worthless."""
    from bench.run import build_cases
    from gateway.bootstrap import build_gateway
    from gateway.db import MEMORY
    from gateway.verify import Outcome, verify_payment_mandate

    gateway = build_gateway(db_path=MEMORY, use_llm=False, sleep=lambda _s: None)
    try:
        cart = gateway.merchant.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}])["cart"]
        checkout = gateway.merchant.create_checkout(cart["cart_id"])
        gateway.merchant.complete_checkout(checkout["checkout_id"], gateway.open_checkout_jws)

        false_accepts = [
            case
            for case in build_cases(gateway, 200)
            if not case.should_allow
            and verify_payment_mandate(
                case.token,
                checkout["checkout_mandate_jwt"],
                gateway.ledger,
                keyring=gateway.keyring,
            ).outcome
            is Outcome.ALLOW
        ]
        assert false_accepts == [], f"FALSE ACCEPTS: {[c.kind for c in false_accepts]}"
    finally:
        gateway.close()


def test_the_benchmark_population_contains_real_adversarial_cases() -> None:
    """A benchmark of 500 valid mandates would measure nothing interesting."""
    from bench.run import build_cases
    from gateway.bootstrap import build_gateway
    from gateway.db import MEMORY

    gateway = build_gateway(db_path=MEMORY, use_llm=False, sleep=lambda _s: None)
    try:
        cases = build_cases(gateway, 300)
        kinds = {c.kind for c in cases}
        assert {"bad_signature", "over_amount_range", "payee_not_allowed", "expired"} <= kinds
        refusable = sum(1 for c in cases if not c.should_allow)
        assert 0.2 < refusable / len(cases) < 0.6, "the mix should be meaningfully adversarial"
    finally:
        gateway.close()


def test_the_benchmark_is_deterministic_in_its_population() -> None:
    """Fixed seed: two runs must generate the same distribution of cases."""
    from bench.run import build_cases
    from gateway.bootstrap import build_gateway
    from gateway.db import MEMORY

    runs = []
    for _ in range(2):
        gateway = build_gateway(db_path=MEMORY, use_llm=False, sleep=lambda _s: None)
        try:
            runs.append([c.kind for c in build_cases(gateway, 120)])
        finally:
            gateway.close()
    assert runs[0] == runs[1]


# ---------------------------------------------------------------------------
# Interop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_third_party_agent_can_complete_a_purchase() -> None:
    """The strongest fidelity claim: an agent that never imported our code."""
    from scenarios.ap2_reference.run import main

    assert await main() == 0


def test_the_reference_agent_imports_nothing_from_this_project() -> None:
    """If it imported our helpers, a matching pair of bugs would cancel out."""
    import ast

    source = (REPO_ROOT / "scenarios" / "ap2_reference" / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"ap2_min", "gateway", "merchant", "shopping_agent", "demo", "llm"}
    assert imported & forbidden == set(), (
        f"the reference agent must be independent, but imports {imported & forbidden}"
    )


def test_the_reference_agent_hardcodes_the_spec_vct_strings() -> None:
    """It types them out from the document, so drift in ours would break interop."""
    from ap2_min import vct
    from scenarios.ap2_reference import agent as ref

    assert ref.VCT_PAYMENT_CLOSED == vct.VCT_PAYMENT_CLOSED == "mandate.payment.1"
    assert ref.VCT_CHECKOUT_OPEN == vct.VCT_CHECKOUT_OPEN == "mandate.checkout.open.1"


def test_an_undelegated_third_party_agent_is_refused_everything(wired: Gateway) -> None:
    """Key binding: interop requires delegation, and that is a feature."""
    from gateway.mandates import generate_keypair
    from scenarios.ap2_reference.agent import ReferenceAgent

    from .test_failure_modes import confirmed_checkout

    private, _ = generate_keypair()
    stranger = ReferenceAgent(private, kid="key_undelegated")
    wired.keyring.register_signer  # noqa: B018 — the key is deliberately NOT registered

    checkout = confirmed_checkout(wired)
    mandate = stranger.closed_payment_mandate(
        payee=checkout["cart"]["merchant_id"],
        payee_name=checkout["cart"]["merchant_name"],
        amount=checkout["cart"]["total"],
        checkout_jws=checkout["checkout_mandate_jwt"],
        open_mandate_jws=wired.open_payment_jws,
    )
    response = wired.merchant.initiate_payment(checkout["checkout_id"], mandate)
    assert response["error"].startswith("mandate.")
    assert isinstance(wired.rail, FakeRail)
    assert wired.rail.captured_total() == 0


def test_delegation_grants_exactly_the_same_authority_and_no_more(wired: Gateway) -> None:
    """Onboarding a third-party agent must not widen a single bound."""
    from ap2_min.models import AmountRangeConstraint, BudgetConstraint
    from gateway.mandates import generate_keypair

    before = wired.open_payment_contents
    private, _ = generate_keypair()
    wired.delegate_to(private.public_key(), kid="key_new_agent")
    after = wired.open_payment_contents

    for name in ("payment.amount_range", "payment.budget", "payment.allowed_payees"):
        old, new = before.constraint(name), after.constraint(name)
        if isinstance(old, AmountRangeConstraint) and isinstance(new, AmountRangeConstraint):
            assert (old.min, old.max) == (new.min, new.max)
        if isinstance(old, BudgetConstraint) and isinstance(new, BudgetConstraint):
            assert old.max == new.max
    assert after.cnf != before.cnf, "only the bound key changed"


# ---------------------------------------------------------------------------
# The HTML timeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_demo_writes_a_self_contained_timeline() -> None:
    """It has to open from file:// on a machine with no network."""
    from demo.batch import TIMELINE_PATH, main_async

    assert await main_async(["--json"]) == 0
    page = TIMELINE_PATH.read_text(encoding="utf-8")

    assert page.startswith("<!doctype html>")
    assert re.search(r'(?:src|href)\s*=\s*["\']https?://', page) is None, "loads a remote resource"
    assert "@import" not in page
    assert page.count("<script") == 1, "one inline script, no external ones"


@pytest.mark.asyncio
async def test_the_timeline_shows_every_audit_row_and_the_report_line() -> None:
    from demo.batch import REPORT_PATH, TIMELINE_PATH, main_async

    await main_async(["--json"])
    page = TIMELINE_PATH.read_text(encoding="utf-8")
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["line"] in page
    assert page.count('<li class="row') == report["audit_rows"]
    assert report["audit_chain_tip"] in page


def test_the_timeline_escapes_hostile_content() -> None:
    """Audit payloads and reasons carry merchant-supplied strings. None are markup.

    The hostile text is written straight into an audit row, because that is the
    only place the renderer meets untrusted content — and a test that relies on a
    hostile product name happening to reach a payload would silently stop
    testing anything the day that payload changed shape.
    """
    from demo.timeline import render
    from gateway.audit import AuditLog, ChainVerification
    from gateway.db import Database

    audit = AuditLog(Database())
    audit.append(
        "merchant",
        "merchant.cart_assembled",
        {"product": "<script>alert('xss')</script>", "url": "javascript:alert(1)"},
        "<img src=x onerror=alert(1)>",
    )

    page = render(
        audit.rows(),
        chain=ChainVerification(ok=True, rows_checked=1),
        tip_hash="a" * 64,
        report_line="<b>not markup</b>",
        report={},
        captured=0,
        rail="fake",
    )

    assert "<script>alert(" not in page
    assert "<img src=x" not in page
    assert "<b>not markup</b>" not in page
    assert "&lt;script&gt;" in page
    assert "&lt;img src=x" in page
    # Exactly one script tag: our own inline filter toggle.
    assert page.count("<script") == 1


# ---------------------------------------------------------------------------
# The Makefile actually exposes all of this
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["redteam", "bench", "interop", "demo", "test", "lint", "smoke"])
def test_every_advertised_make_target_exists(target: str) -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(rf"^{target}:", makefile, re.M), f"make {target} is advertised but missing"
    assert f"make {target}" in makefile, f"make {target} is not in the help output"
