"""Shared fixtures.

Everything here is offline and deterministic. No test in this suite touches the
network, reads a real API key, or depends on wall-clock timing.
"""

from __future__ import annotations

import pytest

from ap2_min.roles import (
    ROLE_MERCHANT,
    ROLE_MPP,
    ROLE_SHOPPING_AGENT,
    ROLE_USER,
)
from gateway.ledger import InMemoryLedgerView
from gateway.mandates import KeyRing, Signer, generate_keypair

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
