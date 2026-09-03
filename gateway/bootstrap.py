"""The composition root: build a fully wired gateway in one call.

Every other module takes its collaborators as constructor arguments and knows
nothing about how they are made. This is the one place that decides which rail,
which database, which keys — which is why the tests, the demo, the MCP server and
the FastAPI service can all be *the same system* configured differently, rather
than three parallel implementations that drift.

Keys are generated in memory at startup and never written to disk. In production
these would come from a KMS or an HSM and the public halves would be published as
a JWKS; here, ephemeral keys keep the demo self-contained and mean there is no
private key in this repository to leak. What the demo actually demonstrates —
that a signature binds an amount to a payee to a cart to a buyer — is unaffected
by where the key came from.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from ap2_min.builders import open_checkout_mandate, open_payment_mandate
from ap2_min.models import CheckoutMandateContents, PaymentMandateContents
from ap2_min.roles import (
    ROLE_MERCHANT,
    ROLE_MPP,
    ROLE_SHOPPING_AGENT,
    ROLE_USER,
)
from gateway.audit import AuditLog
from gateway.config import load_dotenv
from gateway.db import MEMORY, Database
from gateway.ledger import Ledger
from gateway.mandates import KeyRing, Signer, generate_keypair, utcnow
from gateway.payments import PaymentProcessor
from gateway.policy import Policy, load_policy
from gateway.razorpay_client import PaymentRail, build_rail
from gateway.recovery import CircuitBreaker, RecoveryPlaybook
from gateway.trusted_surface import TrustedSurface
from llm.client import LLMClient, LLMUnavailable, build_llm
from llm.reason import ReasonWriter
from merchant.checkout import Catalog, CheckoutStore
from merchant.service import MerchantService


def _signer(kid: str, role: str) -> Signer:
    private_key, _ = generate_keypair()
    return Signer(kid=kid, role=role, private_key=private_key)


@dataclass
class Gateway:
    """Every wired component, plus the buyer's standing authorisations."""

    policy: Policy
    db: Database
    audit: AuditLog
    ledger: Ledger
    keyring: KeyRing

    user: Signer
    agent: Signer
    merchant_signer: Signer
    mpp: Signer

    catalog: Catalog
    store: CheckoutStore
    rail: PaymentRail
    processor: PaymentProcessor
    breaker: CircuitBreaker
    playbook: RecoveryPlaybook
    trusted_surface: TrustedSurface
    merchant: MerchantService
    reasons: ReasonWriter
    llm: LLMClient | None

    #: Signed once, by the simulated buyer, at startup. Everything the agent is
    #: allowed to do flows from exactly these two tokens.
    open_checkout_contents: CheckoutMandateContents
    open_checkout_jws: str
    open_payment_contents: PaymentMandateContents
    open_payment_jws: str

    def close(self) -> None:
        self.db.close()


def build_gateway(
    *,
    db_path: str | Path | None = None,
    rail: PaymentRail | None = None,
    rail_kind: str | None = None,
    seed_path: str | Path | None = None,
    policy_path: str | Path | None = None,
    llm: LLMClient | None = None,
    use_llm: bool | None = None,
    public_url: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Gateway:
    """Build the whole system.

    Defaults are the safe ones: an in-memory database, the fake payment rail, and
    no language model. Every dangerous choice — a real rail, a real API key, a
    file on disk — has to be asked for.
    """
    # `.env` first, so every os.environ.get below sees it. Non-overriding, so a
    # real environment variable still wins.
    load_dotenv()

    policy = load_policy(policy_path)
    database = Database(db_path or os.environ.get("GATEWAY_DB") or MEMORY)
    audit = AuditLog(database)
    ledger = Ledger(database)

    user = _signer("key_user_buyer", ROLE_USER)
    agent = _signer("key_agent_shopper", ROLE_SHOPPING_AGENT)
    merchant_signer = _signer("key_merchant_gateway", ROLE_MERCHANT)
    mpp = _signer("key_mpp_razorpay", ROLE_MPP)

    keyring = KeyRing()
    for signer in (user, agent, merchant_signer, mpp):
        keyring.register_signer(signer)

    catalog = Catalog(seed_path) if seed_path else Catalog()
    store = CheckoutStore(catalog)

    payment_rail = rail if rail is not None else build_rail(rail_kind)
    processor = PaymentProcessor(rail=payment_rail, ledger=ledger, audit=audit, signer=mpp)
    breaker = CircuitBreaker(policy.circuit_breaker)
    playbook = RecoveryPlaybook(
        processor=processor,
        policy=policy.recovery,
        breaker=breaker,
        audit=audit,
        **({"sleep": sleep} if sleep is not None else {}),
    )

    # The Trusted Surface signs *as the buyer*: it models the buyer's own device,
    # which is the only thing entitled to hold that key. Giving it a separate
    # identity would be pretending a website can consent on someone's behalf.
    trusted_surface = TrustedSurface(
        user_signer=user,
        audit=audit,
        public_url=public_url or os.environ.get("GATEWAY_PUBLIC_URL") or "http://127.0.0.1:8000",
    )

    client = _resolve_llm(llm, use_llm)
    reasons = ReasonWriter(client=client, enabled=client is not None)

    merchant = MerchantService(
        catalog=catalog,
        store=store,
        keyring=keyring,
        merchant_signer=merchant_signer,
        playbook=playbook,
        ledger=ledger,
        audit=audit,
        policy=policy,
        trusted_surface=trusted_surface,
        reason_writer=reasons,
    )

    # --- The buyer signs their standing authorisations, once. ---------------
    standing = policy.standing_authorisation
    now = utcnow()
    validity = timedelta(hours=standing.validity_hours)

    checkout_contents = open_checkout_mandate(
        allowed_merchants=list(standing.allowed_payees),
        max_amount=standing.per_txn_max,
        ship_to_pincode=standing.ship_to_pincode,
        cnf=agent.cnf,
    )
    payment_contents = open_payment_mandate(
        budget=standing.daily_budget,
        amount_min=standing.per_txn_min,
        amount_max=standing.per_txn_max,
        allowed_payees=list(standing.allowed_payees),
        not_before=now - timedelta(minutes=1),
        not_after=now + validity,
        # The buyer delegates to THIS agent's key and no other. A copy of the
        # mandate in anyone else's hands is inert.
        cnf=agent.cnf,
    )
    ttl = int(validity.total_seconds())

    return Gateway(
        policy=policy,
        db=database,
        audit=audit,
        ledger=ledger,
        keyring=keyring,
        user=user,
        agent=agent,
        merchant_signer=merchant_signer,
        mpp=mpp,
        catalog=catalog,
        store=store,
        rail=payment_rail,
        processor=processor,
        breaker=breaker,
        playbook=playbook,
        trusted_surface=trusted_surface,
        merchant=merchant,
        reasons=reasons,
        llm=client,
        open_checkout_contents=checkout_contents,
        open_checkout_jws=user.sign(checkout_contents, ttl_seconds=ttl, now=now),
        open_payment_contents=payment_contents,
        open_payment_jws=user.sign(payment_contents, ttl_seconds=ttl, now=now),
    )


def _resolve_llm(llm: LLMClient | None, use_llm: bool | None) -> LLMClient | None:
    """Pick a narration client, and never let that choice break startup.

    ``use_llm=False`` means templates only. ``None`` means "use whatever
    ``$LLM_PROVIDER`` says", which defaults to the deterministic fake. A missing
    or broken API key downgrades to templates with a note — it is narration, and
    narration must never be a startup dependency.
    """
    if llm is not None:
        return llm
    if use_llm is False:
        return None
    try:
        return build_llm()
    except (LLMUnavailable, ValueError) as exc:
        print(f"  note: language model unavailable ({exc}); using deterministic templates.")
        return None
