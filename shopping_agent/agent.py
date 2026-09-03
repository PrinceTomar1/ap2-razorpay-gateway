"""An AP2 Shopping Agent. It can shop. It cannot pay.

The agent's power is exactly this: it holds a keypair, and the buyer signed one
open Checkout Mandate and one open Payment Mandate naming that keypair. Every
purchase it makes is a closed mandate it signs *under* those, and every such
mandate is checked in gateway/verify.py before a rupee moves. The agent has no
card, no credential, no rail access, and no way to raise its own limits.

That is what makes it safe to let a language model drive it. In ``--llm`` mode a
model chooses which SKU to look at. If it chooses badly, the buyer gets the wrong
shoes — annoying, and bounded by the same ₹1,500 cap as everything else. It
cannot choose to spend more, to pay someone else, or to pay twice, because none
of those are things the agent is able to do.

Two modes:

``--scripted``  a fixed plan. Deterministic, and what ``make demo`` runs.
``--llm``       the same plan, with a model picking among the merchant's search
                results. The model's answer is validated against the candidate
                SKUs before it is used; an unrecognised answer falls back to the
                scripted choice rather than being passed through.

The agent talks to the merchant over MCP. In the demo that is an in-process
transport — real protocol, real tool schemas, zero network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ap2_min.builders import closed_payment_mandate
from ap2_min.models import (
    AllowedPayeesConstraint,
    AmountRangeConstraint,
    PaymentMandateContents,
    paise_to_inr_str,
)
from ap2_min.roles import ROLE_SHOPPING_AGENT
from gateway.audit import AuditLog, Event
from gateway.mandates import Signer, utcnow
from llm.client import LLMClient
from shopping_agent.human import HumanGate

#: How many times the agent will re-plan after a merchant says "no such product".
#: Bounded for the same reason everything else here is: an agent that re-plans
#: without limit is an agent that hammers a catalogue forever.
MAX_REPLANS = 2

SELECTION_SYSTEM = (
    "You are a shopping assistant choosing ONE product from a list. Reply with "
    "the SKU only — no explanation, no punctuation, no other words. If nothing "
    "fits, reply NONE."
)


class MerchantTools(Protocol):
    """The seven MCP tools, as the agent sees them."""

    async def search_inventory(
        self, query: str, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...
    async def check_product(self, product_id: str) -> dict[str, Any]: ...
    async def check_serviceability(self, pincode: str) -> dict[str, Any]: ...
    async def assemble_cart(
        self, items: list[dict[str, Any]], ship_to_pincode: str | None = None
    ) -> dict[str, Any]: ...
    async def create_checkout(self, cart_id: str) -> dict[str, Any]: ...
    async def complete_checkout(
        self, checkout_id: str, checkout_mandate_jwt: str
    ) -> dict[str, Any]: ...
    async def initiate_payment(
        self, checkout_id: str, payment_mandate_jwt: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Goal:
    """One thing the buyer asked for, in the agent's own terms."""

    label: str
    query: str
    filters: dict[str, Any] = field(default_factory=dict)
    qty: int = 1
    #: Scripted mode picks this SKU when present, so the demo is reproducible.
    prefer_sku: str | None = None
    #: A SKU the agent has been told to try first, which may not exist. Used to
    #: exercise failure mode 7 without pretending the agent is broken.
    try_sku_first: str | None = None
    instrument: str = "upi"


@dataclass
class AttemptResult:
    """Everything that happened for one goal. The demo's report is built from these."""

    goal: Goal
    status: str
    human_reason: str = ""
    sku: str | None = None
    merchant: str | None = None
    amount: int = 0
    checkout_id: str | None = None
    payment_mandate_id: str | None = None
    receipt: dict[str, Any] | None = None
    receipt_jws: str | None = None
    attempts: int = 0
    recovered: bool = False
    escalated: bool = False
    replans: int = 0
    decision_code: str | None = None
    approval_url: str | None = None

    @property
    def paid(self) -> bool:
        return self.status == "paid"

    @property
    def charged_amount(self) -> int:
        """What actually left the buyer's account for this attempt. Zero unless captured."""
        if self.receipt and self.receipt.get("status") == "captured":
            return int(self.receipt["amount"])
        return 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal.label,
            "status": self.status,
            "sku": self.sku,
            "merchant": self.merchant,
            "amount": self.amount,
            "amount_inr": paise_to_inr_str(self.amount),
            "charged": self.charged_amount,
            "attempts": self.attempts,
            "recovered": self.recovered,
            "escalated": self.escalated,
            "replans": self.replans,
            "decision_code": self.decision_code,
            "human_reason": self.human_reason,
            "receipt_id": (self.receipt or {}).get("receipt_id"),
        }


#: Terminal statuses, and what each one means.
STATUS_PAID = "paid"  # captured, receipt in hand
STATUS_HUMAN_DENIED = "human_denied"  # a person said no at the Trusted Surface
STATUS_DENIED = "denied"  # the verifier refused a bound
STATUS_DECLINED_STOCK = "declined_stock"  # sold out or repriced; nothing charged
STATUS_PAYMENT_FAILED = "payment_failed"  # recovery exhausted; signed failure receipt
STATUS_DEFERRED = "deferred"  # rail unreachable; mandate deliberately unspent
STATUS_NOT_FOUND = "not_found"  # no product matched, after re-planning
STATUS_REJECTED = "rejected"  # our own mandate was refused at the boundary


class ShoppingAgent:
    """Plans purchases, presents mandates, and escalates when it runs out of authority."""

    def __init__(
        self,
        *,
        tools: MerchantTools,
        signer: Signer,
        open_checkout_jws: str,
        open_payment_jws: str,
        open_payment: PaymentMandateContents,
        audit: AuditLog,
        human: HumanGate | None = None,
        llm: LLMClient | None = None,
        mode: str = "scripted",
        ship_to_pincode: str = "560001",
    ) -> None:
        self.tools = tools
        self.signer = signer
        self.open_checkout_jws = open_checkout_jws
        self.open_payment_jws = open_payment_jws
        self.open_payment = open_payment
        self.audit = audit
        self.human = human
        self.llm = llm
        self.mode = mode
        self.ship_to_pincode = ship_to_pincode

    # -- what the agent knows about its own limits -------------------------

    @property
    def per_txn_ceiling(self) -> int | None:
        constraint = self.open_payment.constraint("payment.amount_range")
        return constraint.max if isinstance(constraint, AmountRangeConstraint) else None

    @property
    def allowed_payees(self) -> list[str] | None:
        constraint = self.open_payment.constraint("payment.allowed_payees")
        return constraint.allowed if isinstance(constraint, AllowedPayeesConstraint) else None

    def is_within_standing_scope(self, *, amount: int, merchant_id: str) -> bool:
        """Can the agent authorise this purchase on its own?

        Read from the buyer's signed mandate, not from a config the agent controls.
        Being wrong here is not a security hole — the merchant re-derives all of it
        deterministically — but being *right* is what lets the agent escalate
        politely instead of being refused.
        """
        ceiling = self.per_txn_ceiling
        payees = self.allowed_payees
        if ceiling is not None and amount > ceiling:
            return False
        return not (payees is not None and merchant_id not in payees)

    # -- the purchase -------------------------------------------------------

    async def attempt(
        self, goal: Goal, *, interleave: Callable[[], None] | None = None
    ) -> AttemptResult:
        """Try to buy one thing. Always returns; never raises for a business outcome.

        ``interleave`` runs once, between the merchant signing the cart and the
        payment being presented. That window is not an implementation detail — it
        is the window failure mode 5 exists for, and it is minutes long in
        practice because a human may be approving something in the middle of it.
        A scenario uses this hook to make a concurrent world event happen inside
        it deterministically: another buyer taking the last unit, a price change.
        It injects an *event*, never an outcome; what follows is decided entirely
        by the merchant's own re-check.
        """
        result = AttemptResult(goal=goal, status="pending")
        self.audit.append(
            ROLE_SHOPPING_AGENT,
            Event.AGENT_PLAN,
            {"goal": goal.label, "query": goal.query, "filters": goal.filters},
            f"The agent is looking for: {goal.label}",
        )

        sku = await self._choose_sku(goal, result)
        if sku is None:
            result.status = STATUS_NOT_FOUND
            result.human_reason = "No product in the catalogue matched, after re-planning."
            self.audit.append(
                ROLE_SHOPPING_AGENT,
                Event.AGENT_GAVE_UP,
                {"goal": goal.label, "reason": "no matching product"},
                result.human_reason,
            )
            return result
        result.sku = sku

        # --- Build and price the basket ------------------------------------
        cart_response = await self.tools.assemble_cart(
            [{"sku": sku, "qty": goal.qty}], self.ship_to_pincode
        )
        if "error" in cart_response:
            return self._abandon(result, cart_response["error"], cart_response.get("message", ""))
        cart = cart_response["cart"]
        result.amount = int(cart["total"])
        result.merchant = cart["merchant_name"]

        # --- Get the merchant's signed price guarantee ---------------------
        checkout = await self.tools.create_checkout(cart["cart_id"])
        if "error" in checkout:
            return self._abandon(result, checkout["error"], checkout.get("message", ""))
        result.checkout_id = checkout["checkout_id"]
        checkout_hash_value = checkout["checkout_hash"]

        # --- Present the buyer's standing checkout authorisation -----------
        confirmation = await self.tools.complete_checkout(
            result.checkout_id, self.open_checkout_jws
        )

        if confirmation.get("error") == "unresolved_constraint":
            # The agent has correctly run out of authority. Ask, do not force.
            return await self._escalate(result, confirmation, checkout_hash_value)

        if "error" in confirmation:
            return self._abandon(
                result, confirmation["error"], confirmation.get("message", "") or ""
            )

        # --- A concurrent world event, if the scenario scripted one --------
        # Deliberately here: after the merchant signed the price and confirmed the
        # buyer's authorisation, before payment is presented. That is the window
        # failure mode 5 is about.
        if interleave is not None:
            interleave()

        # --- Sign a closed Payment Mandate for exactly this transaction ----
        payment_jws, mandate_id = self._sign_closed_payment(
            payee=cart["merchant_id"],
            payee_name=cart["merchant_name"],
            amount=result.amount,
            checkout_hash_value=checkout_hash_value,
            open_payment_jws=self.open_payment_jws,
            instrument=goal.instrument,
        )
        result.payment_mandate_id = mandate_id

        response = await self.tools.initiate_payment(result.checkout_id, payment_jws)
        return self._interpret_payment(result, response, checkout_hash_value)

    # -- selection ----------------------------------------------------------

    async def _choose_sku(self, goal: Goal, result: AttemptResult) -> str | None:
        """Pick a SKU, re-planning if the merchant says a product does not exist.

        Failure mode 7 lives here. ``try_sku_first`` lets a scenario hand the agent
        a SKU that is not in the catalogue; ``check_product`` answers
        ``product.not_found``, the agent logs a re-plan and falls back to search.
        Nothing is signed and the verifier is never reached.
        """
        if goal.try_sku_first:
            probe = await self.tools.check_product(goal.try_sku_first)
            if probe.get("error") == "product.not_found":
                result.replans += 1
                self.audit.append(
                    ROLE_SHOPPING_AGENT,
                    Event.AGENT_REPLANNED,
                    {
                        "goal": goal.label,
                        "missing_sku": goal.try_sku_first,
                        "reason": f"{goal.try_sku_first} does not exist; falling back to search",
                    },
                    f"{goal.try_sku_first} is not a real product, so the agent went back to "
                    "search instead of building a cart around it.",
                )
            elif probe.get("found"):
                return str(probe["product"]["sku"])

        for _ in range(MAX_REPLANS):
            found = await self.tools.search_inventory(goal.query, goal.filters)
            candidates: list[dict[str, Any]] = found.get("results", [])
            if not candidates:
                return None
            sku = self._select(goal, candidates)
            if sku is None:
                return None
            # Confirm the SKU really exists and is in stock before committing to
            # it — in `--llm` mode the choice came from a model, and a model's
            # output is a suggestion, not a fact.
            probe = await self.tools.check_product(sku)
            if probe.get("found") and probe["product"]["stock"] > 0:
                return sku
            result.replans += 1
            self.audit.append(
                ROLE_SHOPPING_AGENT,
                Event.AGENT_REPLANNED,
                {"goal": goal.label, "rejected_sku": sku, "reason": "not found or out of stock"},
                f"{sku} turned out to be unavailable, so the agent searched again.",
            )
        return None

    def _select(self, goal: Goal, candidates: list[dict[str, Any]]) -> str | None:
        """Choose among candidates. Scripted picks deterministically; LLM asks a model."""
        skus = [str(c["sku"]) for c in candidates]
        if goal.prefer_sku and goal.prefer_sku in skus:
            return goal.prefer_sku
        if self.mode != "llm" or self.llm is None:
            # Results arrive sorted by price then SKU, so "the first one" is a
            # stable, explainable choice: the cheapest match.
            return skus[0]

        listing = "\n".join(
            f"{c['sku']}  {c['name']}  ₹{c['price_inr']}  stock {c['stock']}" for c in candidates
        )
        try:
            answer = self.llm.complete(
                system=SELECTION_SYSTEM,
                prompt=f"Buyer wants: {goal.label}\n\nAvailable:\n{listing}\n\nSKU:",
                max_tokens=20,
            ).strip()
        except Exception:  # noqa: BLE001 — a model failure must not stop the shop
            return skus[0]

        # The model's answer is validated against the set we offered. This is the
        # whole safety story for LLM mode: its output is constrained to a choice
        # from a list the merchant produced, and an unrecognised answer is
        # discarded rather than passed downstream.
        token = answer.split()[0].strip(".,;:\"'") if answer else ""
        if token in skus:
            return token
        return skus[0]

    # -- mandates -----------------------------------------------------------

    def _sign_closed_payment(
        self,
        *,
        payee: str,
        payee_name: str,
        amount: int,
        checkout_hash_value: str,
        open_payment_jws: str,
        instrument: str,
    ) -> tuple[str, str]:
        """Sign one transaction: this much, to them, for this cart, now.

        The agent's signature proves *who is presenting*; the embedded open
        mandate proves *what the buyer allowed*. Neither alone is sufficient, and
        the agent cannot forge the second.
        """
        now = utcnow()
        contents = closed_payment_mandate(
            payee=payee,
            payee_name=payee_name,
            amount=amount,
            payment_instrument=instrument,
            checkout_hash=checkout_hash_value,
            open_mandate_jws=open_payment_jws,
            execution_date=now,
        )
        return (
            self.signer.sign(contents, ttl_seconds=600, now=now),
            contents.mandate_id,
        )

    # -- escalation ---------------------------------------------------------

    async def _escalate(
        self, result: AttemptResult, unresolved: dict[str, Any], checkout_hash_value: str
    ) -> AttemptResult:
        """Hand an unresolved constraint to a human and act on the answer.

        The agent can reach the surface and read the outcome. It cannot decide it:
        see :mod:`shopping_agent.human`.
        """
        result.escalated = True
        result.decision_code = unresolved.get("constraint")
        result.approval_url = unresolved.get("approval_url")
        reason = unresolved.get("human_reason", "This purchase needs approval.")
        self.audit.append(
            ROLE_SHOPPING_AGENT,
            Event.AGENT_ESCALATED,
            {
                "checkout_id": result.checkout_id,
                "constraint": result.decision_code,
                "amount": result.amount,
                "reason": reason,
                "approval_url": result.approval_url,
            },
            f"The agent stopped and asked for approval: {reason}",
        )

        hold_id = unresolved.get("hold_id")
        if self.human is None or not hold_id:
            result.status = STATUS_HUMAN_DENIED
            result.human_reason = (
                "Approval was required but no Trusted Surface was reachable, so the "
                "purchase was abandoned. Nothing was charged."
            )
            return result

        decision = await self.human.await_decision(hold_id, approval_url=result.approval_url or "")

        if decision.get("status") != "approved":
            result.status = STATUS_HUMAN_DENIED
            result.human_reason = (
                f"The buyer declined ₹{paise_to_inr_str(result.amount)} at "
                f"{result.merchant}. Nothing was charged."
            )
            self.audit.append(
                ROLE_SHOPPING_AGENT,
                Event.AGENT_GAVE_UP,
                {
                    "checkout_id": result.checkout_id,
                    "reason": "the buyer declined",
                    "amount": result.amount,
                },
                result.human_reason,
            )
            return result

        # Approved. The surface handed back mandates the *buyer* signed; the agent
        # relays them and nothing more.
        user_checkout_jws = decision.get("checkout_mandate_jws")
        user_payment_jws = decision.get("payment_mandate_jws")
        if not user_checkout_jws or not user_payment_jws:  # pragma: no cover — defensive
            result.status = STATUS_HUMAN_DENIED
            result.human_reason = "The approval did not produce usable mandates."
            return result

        assert result.checkout_id is not None
        confirmation = await self.tools.complete_checkout(result.checkout_id, user_checkout_jws)
        if "error" in confirmation:
            return self._abandon(
                result, confirmation["error"], confirmation.get("human_reason", "") or ""
            )
        response = await self.tools.initiate_payment(result.checkout_id, user_payment_jws)
        return self._interpret_payment(result, response, checkout_hash_value)

    # -- responses ----------------------------------------------------------

    def _interpret_payment(
        self, result: AttemptResult, response: dict[str, Any], checkout_hash_value: str
    ) -> AttemptResult:
        """Map the merchant's structured answer onto a terminal status.

        Every branch here is a distinct, documented outcome. There is no `else:
        assume it worked` — an agent that cannot tell "paid" from "we do not know"
        is the agent that double-charges.
        """
        if response.get("error") == "unresolved_constraint":
            result.escalated = True
            result.decision_code = response.get("constraint")
            result.status = STATUS_HUMAN_DENIED
            result.human_reason = response.get("human_reason", "This purchase needs approval.")
            result.approval_url = response.get("approval_url")
            return result

        if response.get("error") == "stock.unavailable":
            result.status = STATUS_DECLINED_STOCK
            result.decision_code = "stock.unavailable"
            result.human_reason = response.get("message", "The item is no longer available.")
            self.audit.append(
                ROLE_SHOPPING_AGENT,
                Event.AGENT_GAVE_UP,
                {"checkout_id": result.checkout_id, "reason": result.human_reason},
                f"The agent abandoned this purchase: {result.human_reason}",
            )
            return result

        if response.get("error") == "denied":
            result.status = STATUS_DENIED
            result.decision_code = response.get("code")
            result.human_reason = response.get("human_reason", "The payment was refused.")
            return result

        if "error" in response:
            result.status = STATUS_REJECTED
            result.decision_code = response["error"]
            result.human_reason = response.get("message", "The mandate was rejected.")
            return result

        if response.get("status") == "deferred":
            result.status = STATUS_DEFERRED
            result.human_reason = response.get("human_reason", "The payment rail is unreachable.")
            return result

        receipt = response.get("payment_receipt")
        if receipt is None:  # pragma: no cover — defensive
            result.status = STATUS_REJECTED
            result.human_reason = "The merchant returned no receipt."
            return result

        result.receipt = receipt
        result.receipt_jws = response.get("payment_receipt_jws")
        result.attempts = int(response.get("attempts", receipt.get("attempts", 1)))
        result.recovered = bool(response.get("recovered", False))
        result.human_reason = response.get("human_reason", "")
        if receipt.get("status") == "captured":
            result.status = STATUS_PAID
            # A receipt that does not bind to the checkout we asked to pay for is
            # not a receipt for our purchase. Cheap to check, and the agent is the
            # last party able to catch a merchant-side mix-up.
            assert receipt["checkout_hash"] == checkout_hash_value
        else:
            result.status = STATUS_PAYMENT_FAILED
            result.decision_code = receipt.get("failure_code")
        return result

    def _abandon(self, result: AttemptResult, code: str, message: str) -> AttemptResult:
        # A basket that sold out is a distinct, expected outcome — not the same
        # thing as a mandate being refused, and the caller needs to tell them
        # apart to report honestly.
        result.status = STATUS_DECLINED_STOCK if code == "stock.unavailable" else STATUS_REJECTED
        result.decision_code = code
        result.human_reason = message or f"The merchant refused: {code}"
        self.audit.append(
            ROLE_SHOPPING_AGENT,
            Event.AGENT_GAVE_UP,
            {"goal": result.goal.label, "code": code, "reason": result.human_reason},
            f"The agent abandoned this purchase: {result.human_reason}",
        )
        return result
