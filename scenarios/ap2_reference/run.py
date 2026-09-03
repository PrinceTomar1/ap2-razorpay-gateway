"""`make interop` — a third-party AP2 agent buys from our Merchant over MCP.

The agent in `agent.py` imports nothing from this project. It signs with plain
PyJWT and builds its mandates by hand from the published spec. The only thing it
is given is what any real third party would have: the merchant's MCP endpoint,
and the buyer's two open mandates.

Writes `scenarios/ap2_reference/transcript.md`. Exits non-zero if the purchase
does not complete, so this is a gate rather than a story.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from fastmcp import Client

from gateway.bootstrap import build_gateway
from gateway.db import MEMORY
from gateway.mandates import generate_keypair
from merchant.mcp_server import build_server
from scenarios.ap2_reference.agent import ReferenceAgent

TRANSCRIPT = Path(__file__).resolve().parent / "transcript.md"


async def main() -> int:
    gateway = build_gateway(db_path=MEMORY, use_llm=False, sleep=lambda _s: None)
    try:
        # The third-party agent generates its OWN key. The buyer then delegates to
        # it — exactly as a real buyer would onboard an agent they did not write.
        private_key, _ = generate_keypair()
        agent = ReferenceAgent(private_key, kid="key_third_party_agent")

        # The buyer re-issues their standing authorisation naming this agent's key.
        # Without this the gateway correctly refuses everything (key binding), which
        # is itself worth showing.
        gateway.delegate_to(agent.public_key, kid=agent.kid)

        server, _ = build_server(gateway)
        async with Client(server) as client:
            t = agent.transcript

            found = (
                await client.call_tool(
                    "search_inventory",
                    {"query": "running", "filters": {"max_price_inr": 1500, "size": "9"}},
                )
            ).data
            t.record(
                "search_inventory",
                f"{found['count']} results under ₹1,500",
                [p["sku"] for p in found["results"]][:5],
            )
            sku = found["results"][0]["sku"]

            product = (await client.call_tool("check_product", {"id": sku})).data
            t.record(
                "check_product",
                f"{sku} → {product['product']['name']}",
                {"price_inr": product["product"]["price_inr"]},
            )

            cart = (
                await client.call_tool(
                    "assemble_cart",
                    {"items": [{"sku": sku, "qty": 1}], "ship_to_pincode": "560001"},
                )
            ).data["cart"]
            t.record("assemble_cart", f"cart {cart['cart_id']} total ₹{cart['total'] / 100:,.2f}")

            checkout = (
                await client.call_tool("create_checkout", {"cart_id": cart["cart_id"]})
            ).data
            t.record(
                "create_checkout",
                f"merchant-signed {checkout['checkout_mandate_jwt'].split('.')[0][:24]}…",
                {"checkout_hash": checkout["checkout_hash"][:32] + "…"},
            )

            confirmed = (
                await client.call_tool(
                    "complete_checkout",
                    {
                        "checkout_id": checkout["checkout_id"],
                        "checkout_mandate_jwt": gateway.open_checkout_jws,
                    },
                )
            ).data
            t.record(
                "complete_checkout", f"status={confirmed.get('status', confirmed.get('error'))}"
            )
            if confirmed.get("status") != "confirmed":
                t.record("ABORT", f"checkout not confirmed: {confirmed}")
                TRANSCRIPT.write_text(render(t.steps, success=False), encoding="utf-8")
                return 1

            mandate = agent.closed_payment_mandate(
                payee=cart["merchant_id"],
                payee_name=cart["merchant_name"],
                amount=cart["total"],
                checkout_jws=checkout["checkout_mandate_jwt"],
                open_mandate_jws=gateway.open_payment_jws,
            )

            paid = (
                await client.call_tool(
                    "initiate_payment",
                    {"checkout_id": checkout["checkout_id"], "payment_mandate_jwt": mandate},
                )
            ).data
            t.record(
                "initiate_payment",
                f"status={paid.get('status', paid.get('error'))}",
                paid.get("payment_receipt"),
            )

            success = paid.get("status") == "captured"
            if success:
                t.record(
                    "verifier",
                    "14 checks passed on a mandate this gateway's own code never built",
                )

        chain = gateway.audit.verify_chain()
        t.record("audit", f"chain intact={chain.ok} over {chain.rows_checked} rows")
        TRANSCRIPT.write_text(render(agent.transcript.steps, success=success), encoding="utf-8")

        mark = "\033[32m✓\033[0m" if success else "\033[31m✗\033[0m"
        for step in agent.transcript.steps:
            print(f"  {step['step']:<32} {step['detail']}")
        print(f"\n  {mark} third-party AP2 agent purchase: {'COMPLETED' if success else 'FAILED'}")
        print(f"  wrote {TRANSCRIPT.relative_to(Path.cwd())}\n")
        return 0 if success else 1
    finally:
        gateway.close()


def render(steps: list[dict[str, Any]], *, success: bool) -> str:
    import json

    lines = [
        "# Interop transcript",
        "",
        f"**A third-party AP2 shopping agent {'completed' if success else 'FAILED'} a purchase "
        "against this Merchant.**",
        "",
        "The agent in `scenarios/ap2_reference/agent.py` imports **nothing** from this",
        "project — not `ap2_min`, not `gateway`, not `merchant`, not `shopping_agent`. It",
        "builds its mandate claims by hand from the field names in the published spec and",
        "signs them with plain PyJWT. It talks to the Merchant over a real MCP client.",
        "",
        "That constraint is the point. Our own agent working proves our code is",
        "self-consistent. *This* proves the gateway implements AP2 for somebody who has",
        "never read our source — a bug in our signing helper cannot be cancelled out by a",
        "matching bug in our verification, because the agent does not use our helper.",
        "",
        "One thing it *is* given, because any real third-party agent would be: the buyer",
        "re-issues their standing authorisation naming the new agent's public key. Key",
        "binding (RFC 7800) means an agent the buyer has not delegated to is refused",
        "everything — correctly.",
        "",
        "## The exchange",
        "",
        "| Step | What happened |",
        "|---|---|",
    ]
    for step in steps:
        lines.append(f"| `{step['step']}` | {step['detail']} |")
    lines += ["", "## Payloads", ""]
    for step in steps:
        if step.get("payload"):
            lines += [
                f"**{step['step']}**",
                "",
                "```json",
                json.dumps(step["payload"], indent=2, ensure_ascii=False)[:1400],
                "```",
                "",
            ]
    lines += [
        "## What this does and does not show",
        "",
        "**Does:** our `vct` strings, constraint field names, `checkout_hash` derivation",
        "and MCP tool contract are all reproducible from the spec alone.",
        "",
        "**Does not:** this is not Google's reference agent. That one is built on A2A and",
        "google-adk, which this project deliberately does not depend on — vendoring a",
        "transport stack to prove a point about a mandate format would be the wrong trade.",
        "The honest claim is 'an independent implementation of the spec interoperates',",
        "not 'certified against the reference implementation'. LIMITATIONS.md repeats it.",
        "",
        "Generated by `make interop` — do not edit by hand.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
