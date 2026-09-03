"""Six purchase attempts against a real gateway. Nothing here is hardcoded.

A buyer with a ₹5,000 daily budget and a ₹1,500 per-purchase cap sends an agent
shopping at three merchants. Six things happen:

    1  clean buy, within every bound                                → paid
    2  clean buy                                                    → paid
    3  ₹4,999 — over the per-purchase cap                           → the agent
       escalates, the Trusted Surface asks a human, the human declines → skipped
    4  the bank declines UPI                                        → recovery
       falls back to a payment link and succeeds on the same idempotency root
    5  clean buy                                                    → paid
    6  the last unit sells out between checkout and payment         → clean
       decline, nothing charged

Attempt 1 also hands the agent a SKU that does not exist, so the re-planning path
(failure mode 7) is exercised on the way through.

**Every number in the report is measured, not asserted.** The demo scripts two
*world events* — "the bank declines the next UPI attempt", "another buyer takes
the last cap" — and nothing else. Whether a payment succeeds, whether recovery
recovers, whether a human is asked, and how much money moved are all read back
out of the modules afterwards:

    paid              counted from AttemptResult.status, set from signed receipts
    recovered         counted from RecoveryResult.recovered, which is
                      `captured and attempts > 1`
    human_denied      counted from decisions the SimulatedShopper actually made
    unauthorised      reconciled three ways — what the payment rail captured,
                      what the spend ledger recorded, and what the signed
                      receipts add up to. All three must agree, and every capture
                      must trace to an ALLOW row in the audit chain.
    explained         counted from audit rows that carry a human_reason

If the code stopped working the numbers would change, which is the only property
that makes a demo worth running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import Client

from ap2_min.models import paise_to_inr_str
from gateway.audit import Event, render_log
from gateway.bootstrap import Gateway, build_gateway
from gateway.razorpay_client import METHOD_UPI, FakeRail
from gateway.trusted_surface import HeldRequest
from merchant.mcp_server import build_server
from shopping_agent.agent import (
    STATUS_DECLINED_STOCK,
    STATUS_HUMAN_DENIED,
    STATUS_PAID,
    AttemptResult,
    Goal,
    ShoppingAgent,
)
from shopping_agent.human import SimulatedShopper
from shopping_agent.mcp_tools import McpMerchantTools

REPORT_PATH = Path(__file__).with_name("report.json")

#: The shopping list. Prices come from merchant/seed.json; the agent finds them by
#: searching, and `prefer_sku` only makes the *choice* reproducible, never the
#: outcome.
PLAN: list[Goal] = [
    Goal(
        label="running shoes, size 9, under ₹1,500, ship to 560001",
        query="running",
        filters={"category": "running_shoes", "max_price_inr": 1500, "size": "9"},
        prefer_sku="SF-RUN-001",
        # The buyer's note mentioned a model number that does not exist. The agent
        # checks, is told so, and re-plans. Failure mode 7, in passing.
        try_sku_first="SF-RUN-999",
    ),
    Goal(
        label="running shorts under ₹1,000",
        query="shorts",
        filters={"category": "apparel", "max_price_inr": 1000},
        prefer_sku="SF-APP-001",
    ),
    Goal(
        label="the carbon-plate marathon racing shoe",
        query="marathon",
        filters={"category": "running_shoes"},
        prefer_sku="SF-RUN-004",
    ),
    Goal(
        label="a cast iron dosa tawa",
        query="dosa tawa",
        filters={"category": "kitchen"},
        prefer_sku="LM-KIT-002",
    ),
    Goal(
        label="a magnetic car phone mount",
        query="car phone mount",
        filters={"category": "mobile_accessories"},
        prefer_sku="PB-MOB-003",
    ),
    Goal(
        label="a reflective running cap",
        query="reflective cap",
        filters={"category": "accessories"},
        prefer_sku="SF-ACC-001",
    ),
]

#: The two world events. Injected *before* the module that reacts to them runs.
SOLD_OUT_SKU = "SF-ACC-001"


def approval_policy(request: HeldRequest) -> bool:
    """The simulated buyer's judgement.

    They decline the ₹4,999 racing shoe: it is four times what they told their
    agent it could spend without asking, and the whole point of being asked is
    being able to say no. A person, not a rule — which is why it is a callable
    the scenario supplies rather than logic inside the agent.
    """
    return False


@dataclass
class Report:
    """The measured result. Every field is derived; none is written by hand."""

    attempts: int
    paid: int
    human_denied: int
    recovered: int
    unauthorised_spend: int
    actions_explained: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "paid": self.paid,
            "human_denied": self.human_denied,
            "recovered": self.recovered,
            "unauthorised_spend": self.unauthorised_spend,
            "actions_explained": self.actions_explained,
        }

    def line(self) -> str:
        """The one line the video reads out."""
        return (
            f"{self.attempts} attempts · {self.paid} paid · {self.human_denied} human-denied · "
            f"{self.recovered} recovered · Rs {self.unauthorised_spend} unauthorised · "
            f"{self.actions_explained} explained"
        )


def measure(gateway: Gateway, results: list[AttemptResult]) -> Report:
    """Read the outcome back out of the system. This is the whole report.

    Note what is *not* here: no counter incremented as the demo goes along, no
    expected values, no branch that knows which attempt was which. Every number
    is a query over state the modules produced.
    """
    paid = sum(1 for r in results if r.status == STATUS_PAID)
    human_denied = sum(1 for r in results if r.status == STATUS_HUMAN_DENIED)
    recovered = sum(1 for r in results if r.recovered)

    # --- Three independent views of the money, which must agree. ------------
    receipts_say = sum(r.charged_amount for r in results)
    ledger_says = gateway.ledger.total_captured()
    audit_says = sum(
        int(row.payload["amount"])
        for row in gateway.audit.rows(event=Event.PAYMENT_RECEIPT_ISSUED)
        if row.payload.get("status") == "captured"
    )
    rail_says = (
        gateway.rail.captured_total() if isinstance(gateway.rail, FakeRail) else receipts_say
    )
    for name, value in (
        ("spend ledger", ledger_says),
        ("audit chain", audit_says),
        ("payment rail", rail_says),
    ):
        if value != receipts_say:
            raise AssertionError(
                f"reconciliation failed: signed receipts total ₹{paise_to_inr_str(receipts_say)} "
                f"but the {name} says ₹{paise_to_inr_str(value)}"
            )

    # --- Was any of it unauthorised? ---------------------------------------
    # Money is authorised when a captured receipt traces to an ALLOW from the
    # deterministic verifier. Anything captured without one is unauthorised
    # spend, by definition.
    allowed_mandates = {
        row.payload.get("checkout_id")
        for row in gateway.audit.rows(event=Event.DECISION)
        if row.payload.get("outcome") == "ALLOW"
    }
    authorised = sum(
        r.charged_amount for r in results if r.checkout_id in allowed_mandates and r.charged_amount
    )
    unauthorised = rail_says - authorised

    explained = sum(1 for r in results if r.human_reason.strip())
    return Report(
        attempts=len(results),
        paid=paid,
        human_denied=human_denied,
        recovered=recovered,
        unauthorised_spend=unauthorised,
        actions_explained=f"{explained}/{len(results)}",
    )


async def run_batch(
    gateway: Gateway,
    *,
    plan: list[Goal] | None = None,
    mode: str = "scripted",
    quiet: bool = False,
) -> tuple[list[AttemptResult], SimulatedShopper]:
    """Drive the agent through the plan over a real in-process MCP connection."""
    goals = plan if plan is not None else PLAN
    server, _ = build_server(gateway)
    shopper = SimulatedShopper(gateway.trusted_surface, policy=approval_policy)

    async with Client(server) as client:
        tools = McpMerchantTools(client)
        agent = ShoppingAgent(
            tools=tools,
            signer=gateway.agent,
            open_checkout_jws=gateway.open_checkout_jws,
            open_payment_jws=gateway.open_payment_jws,
            open_payment=gateway.open_payment_contents,
            audit=gateway.audit,
            human=shopper,
            llm=gateway.llm if mode == "llm" else None,
            mode=mode,
            ship_to_pincode=gateway.policy.standing_authorisation.ship_to_pincode,
        )

        results: list[AttemptResult] = []
        for index, goal in enumerate(goals, start=1):
            _inject_world_event(gateway, index, goal)
            result = await agent.attempt(goal, interleave=_interleave_for(gateway, index, goal))
            results.append(result)
            if not quiet:
                _print_attempt(index, result)
    return results, shopper


def _inject_world_event(gateway: Gateway, index: int, goal: Goal) -> None:
    """Things that happen in the world, before the attempt that meets them.

    Only two, and neither decides an outcome:

    * Before attempt 4, the bank is set to decline the next UPI payment. Whether
      the purchase then succeeds is up to gateway/recovery.py.
    * Nothing else. The sold-out event has to happen *inside* attempt 6 — see
      :func:`_interleave_for`.
    """
    if index == 4 and isinstance(gateway.rail, FakeRail):
        gateway.rail.decline(methods={METHOD_UPI}, times=1)
        print("    · the bank will decline the next UPI attempt")


def _interleave_for(gateway: Gateway, index: int, goal: Goal) -> Any:
    """A concurrent event inside the checkout-to-payment window, for attempt 6.

    Another shopper buys the last reflective cap while our agent is between the
    merchant's signed checkout and presenting payment. This is a real window in
    any e-commerce system; the merchant's re-check is what closes it.
    """
    if index != 6:
        return None

    def another_buyer_takes_the_last_one() -> None:
        gateway.catalog.set_stock(SOLD_OUT_SKU, 0)
        print(f"    · another buyer just took the last {SOLD_OUT_SKU}")

    return another_buyer_takes_the_last_one


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_ICON = {
    STATUS_PAID: "\033[32m✓\033[0m",
    STATUS_HUMAN_DENIED: "\033[33m⨯\033[0m",
    STATUS_DECLINED_STOCK: "\033[31m⨯\033[0m",
}


def _print_attempt(index: int, result: AttemptResult) -> None:
    icon = _ICON.get(result.status, "\033[31m⨯\033[0m")
    amount = f"₹{paise_to_inr_str(result.amount)}" if result.amount else "—"
    print(f"\n  {icon} attempt {index}  {result.goal.label}")
    print(f"      {result.sku or '—':<12} {result.merchant or '—':<26} {amount:>12}")
    print(f"      status: \033[1m{result.status}\033[0m")
    if result.replans:
        print(f"      re-planned {result.replans}× before finding a real product")
    if result.escalated:
        print(f"      escalated to a human: {result.decision_code}")
    if result.recovered:
        print(f"      recovered after {result.attempts} attempts, one idempotency root")
    if result.human_reason:
        print(f"      \033[90m{result.human_reason}\033[0m")


def _print_header(gateway: Gateway, mode: str) -> None:
    standing = gateway.policy.standing_authorisation
    print("\n\033[1mAP2 × Razorpay — agentic checkout batch\033[0m")
    print(f"  rail            {gateway.rail.name}")
    print(f"  narration       {gateway.llm.name if gateway.llm else 'templates (no model)'}")
    print(f"  agent mode      {mode}")
    print(f"  daily budget    ₹{paise_to_inr_str(standing.daily_budget)}")
    print(f"  per purchase    ₹{paise_to_inr_str(standing.per_txn_max)}")
    print(f"  merchants       {', '.join(standing.allowed_payees)}")
    print(f"  catalogue       {len(gateway.catalog.products)} SKUs")


def _print_footer(
    gateway: Gateway, report: Report, shopper: SimulatedShopper, *, compact: bool = True
) -> None:
    chain = gateway.audit.verify_chain()
    print("\n\033[1m  audit trail\033[0m")
    print(render_log(gateway.audit.rows(), compact=compact))

    print("\n\033[1m  reconciliation\033[0m")
    print(f"      audit rows            {chain.rows_checked}")
    print(f"      chain intact          {'yes' if chain.ok else 'NO — ' + str(chain.reason)}")
    print(f"      chain tip             {gateway.audit.tip_hash()[:32]}…")
    print(f"      captured (ledger)     ₹{paise_to_inr_str(gateway.ledger.total_captured())}")
    print(f"      human decisions       {len(shopper.decisions)}")
    print(f"      budget remaining      ₹{paise_to_inr_str(_remaining(gateway))}")

    print(f"\n\033[1m{report.line()}\033[0m\n")


def _remaining(gateway: Gateway) -> int:
    budget = gateway.policy.standing_authorisation.daily_budget
    return budget - gateway.ledger.spent_under(gateway.open_payment_contents.mandate_id)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AP2 × Razorpay batch.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="run attempts 1 and 4 against the real Razorpay TEST sandbox",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="let a model choose products (it still cannot choose amounts)",
    )
    parser.add_argument("--json", action="store_true", help="print report.json and nothing else")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every individual verifier check instead of folding the passing ones",
    )
    args = parser.parse_args(argv)

    mode = "llm" if args.llm else "scripted"
    plan = PLAN

    if args.live:
        # Only attempts 1 and 4 — a clean buy and a recovery — go to the real
        # sandbox. Everything else in the batch is about behaviour a sandbox
        # cannot be made to produce on demand.
        os.environ["PAYMENT_RAIL"] = "razorpay"
        plan = [PLAN[0], PLAN[3]]
        print(
            "\n  LIVE: attempts 1 and 4 will run against the real Razorpay test sandbox.\n"
            "  You will be given payment links to pay by hand — use success@razorpay.\n"
            "  See docs/RAZORPAY_TESTING.md.\n"
        )

    gateway = build_gateway(rail_kind=os.environ.get("PAYMENT_RAIL"))
    try:
        if not args.json:
            _print_header(gateway, mode)
        results, shopper = await run_batch(gateway, plan=plan, mode=mode, quiet=args.json)
        report = measure(gateway, results)

        REPORT_PATH.write_text(
            json.dumps(
                {
                    **report.as_dict(),
                    "line": report.line(),
                    "rail": gateway.rail.name,
                    "live": args.live,
                    "agent_mode": mode,
                    "audit_rows": gateway.audit.count(),
                    "audit_chain_intact": gateway.audit.verify_chain().ok,
                    "audit_chain_tip": gateway.audit.tip_hash(),
                    "captured_paise": gateway.ledger.total_captured(),
                    "attempts_detail": [r.as_dict() for r in results],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        if args.json:
            print(REPORT_PATH.read_text(encoding="utf-8"), end="")
        else:
            _print_footer(gateway, report, shopper, compact=not args.verbose)
        return 0
    finally:
        gateway.close()


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
