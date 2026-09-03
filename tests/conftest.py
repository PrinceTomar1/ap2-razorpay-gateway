"""Shared fixtures.

Everything here is offline and deterministic. No test in this suite touches the
network, reads a real API key, or depends on wall-clock timing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ap2_min.models import CheckoutMandateContents
from ap2_min.roles import (
    ROLE_MERCHANT,
    ROLE_MPP,
    ROLE_SHOPPING_AGENT,
    ROLE_USER,
)
from gateway.audit import AuditLog
from gateway.bootstrap import Gateway, build_gateway
from gateway.db import MEMORY, Database
from gateway.ledger import InMemoryLedgerView, Ledger
from gateway.mandates import KeyRing, Signer, generate_keypair, load_checkout_mandate
from gateway.payments import PaymentProcessor
from gateway.policy import Policy, load_policy
from gateway.razorpay_client import FakeRail
from gateway.recovery import CircuitBreaker, RecoveryPlaybook
from gateway.verify import Decision, verify_payment_mandate
from shopping_agent.human import SimulatedShopper, always_approve

from .factories import Scenario

#: Credentials that must never be visible to a test. Deleted outright.
_SECRET_ENV_VARS = (
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "ANTHROPIC_API_KEY",
)

#: Configuration pinned to safe, explicit values. Setting rather than deleting is
#: deliberate: build_gateway() calls load_dotenv(), and load_dotenv only fills in
#: names that are ABSENT from the environment. Deleting these would invite the
#: developer's own .env to supply them again — which is exactly the bug this
#: fixture was written to close.
_PINNED_ENV = {
    "PAYMENT_RAIL": "fake",
    "LLM_PROVIDER": "fake",
    "GATEWAY_DB": MEMORY,
    "POLICY_FILE": "config/policy.yaml",
    "GATEWAY_PUBLIC_URL": "http://127.0.0.1:8000",
}


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as if no .env and no exported configuration existed.

    Added after honouring .env made three tests fail: `GATEWAY_DB=run/gateway.db`
    from the shipped .env.example turned the in-memory test database into a shared
    file on disk, so tests polluted one another and accumulated real state across
    runs. A test suite whose result depends on the developer's local
    configuration is not a test suite.
    """
    for name in _SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in _PINNED_ENV.items():
        monkeypatch.setenv(name, value)


def make_signer(kid: str, role: str) -> Signer:
    private_key, _ = generate_keypair()
    return Signer(kid=kid, role=role, private_key=private_key)


@pytest.fixture
def user_signer() -> Signer:
    return make_signer("key_user_1", ROLE_USER)


@pytest.fixture
def agent_signer() -> Signer:
    return make_signer("key_agent_1", ROLE_SHOPPING_AGENT)


@pytest.fixture
def merchant_signer() -> Signer:
    return make_signer("key_merchant_1", ROLE_MERCHANT)


@pytest.fixture
def mpp_signer() -> Signer:
    return make_signer("key_mpp_1", ROLE_MPP)


@pytest.fixture
def keyring(
    user_signer: Signer,
    agent_signer: Signer,
    merchant_signer: Signer,
    mpp_signer: Signer,
) -> KeyRing:
    ring = KeyRing()
    for signer in (user_signer, agent_signer, merchant_signer, mpp_signer):
        ring.register_signer(signer)
    return ring


@pytest.fixture
def scenario(user_signer: Signer, agent_signer: Signer, merchant_signer: Signer) -> Scenario:
    return Scenario(user=user_signer, agent=agent_signer, merchant=merchant_signer)


@pytest.fixture
def ledger_view() -> InMemoryLedgerView:
    return InMemoryLedgerView()


# ---------------------------------------------------------------------------
# A wired gateway, backed by an in-memory database and the fake rail.
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Database:
    return Database()


@pytest.fixture
def audit(db: Database) -> AuditLog:
    return AuditLog(db)


@pytest.fixture
def ledger(db: Database) -> Ledger:
    return Ledger(db)


@pytest.fixture
def rail() -> FakeRail:
    return FakeRail()


@pytest.fixture
def processor(
    rail: FakeRail, ledger: Ledger, audit: AuditLog, mpp_signer: Signer
) -> PaymentProcessor:
    return PaymentProcessor(rail=rail, ledger=ledger, audit=audit, signer=mpp_signer)


@pytest.fixture
def policy() -> Policy:
    return load_policy("config/policy.yaml")


@pytest.fixture
def breaker(policy: Policy) -> CircuitBreaker:
    return CircuitBreaker(policy.circuit_breaker, clock=lambda: 0.0)


@pytest.fixture
def playbook(
    processor: PaymentProcessor, policy: Policy, breaker: CircuitBreaker, audit: AuditLog
) -> RecoveryPlaybook:
    # sleep is a no-op: bounded recovery is about attempt counts, not wall clock,
    # and a test suite that actually sleeps is a test suite nobody runs.
    return RecoveryPlaybook(
        processor=processor,
        policy=policy.recovery,
        breaker=breaker,
        audit=audit,
        sleep=lambda _seconds: None,
    )


@pytest.fixture
def allow(scenario: Scenario, keyring: KeyRing, ledger: Ledger) -> Decision:
    """A genuine ALLOW, produced by the real verifier over the real ledger."""
    decision = verify_payment_mandate(
        scenario.present(), scenario.checkout_jws, ledger, keyring=keyring, now=scenario.now
    )
    assert decision.allowed, decision.human_reason
    return decision


@pytest.fixture
def checkout_contents(scenario: Scenario, keyring: KeyRing) -> CheckoutMandateContents:
    contents, _ = load_checkout_mandate(scenario.checkout_jws, keyring)
    return contents


# ---------------------------------------------------------------------------
# A whole gateway, wired the way the demo wires it.
# ---------------------------------------------------------------------------


@pytest.fixture
def wired() -> Iterator[Gateway]:
    """A complete system: catalogue, merchant, verifier, processor, gate, audit.

    Offline and deterministic — the fake rail, no language model, an in-memory
    database, and a no-op sleep so bounded recovery never actually waits.
    """
    gateway = build_gateway(db_path=MEMORY, use_llm=False, sleep=lambda _seconds: None)
    try:
        yield gateway
    finally:
        gateway.close()


@pytest.fixture
def fake_rail(wired: Gateway) -> FakeRail:
    assert isinstance(wired.rail, FakeRail)
    return wired.rail


@pytest.fixture
def shopper(wired: Gateway) -> SimulatedShopper:
    """A simulated buyer who approves. Override `.policy` to make them decline."""
    return SimulatedShopper(wired.trusted_surface, policy=always_approve)
