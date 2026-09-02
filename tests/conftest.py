"""Shared fixtures.

Everything here is offline and deterministic. No test in this suite touches the
network, reads a real API key, or depends on wall-clock timing.
"""

from __future__ import annotations

import pytest

from ap2_min.models import CheckoutMandateContents
from ap2_min.roles import (
    ROLE_MERCHANT,
    ROLE_MPP,
    ROLE_SHOPPING_AGENT,
    ROLE_USER,
)
from gateway.audit import AuditLog
from gateway.db import Database
from gateway.ledger import InMemoryLedgerView, Ledger
from gateway.mandates import KeyRing, Signer, generate_keypair, load_checkout_mandate
from gateway.payments import PaymentProcessor
from gateway.policy import Policy, load_policy
from gateway.razorpay_client import FakeRail
from gateway.recovery import CircuitBreaker, RecoveryPlaybook
from gateway.verify import Decision, verify_payment_mandate

from .factories import Scenario


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
