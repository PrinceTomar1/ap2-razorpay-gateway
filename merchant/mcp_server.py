"""The Merchant role as an MCP server: exactly seven tools, and nothing else.

Any AP2 shopping agent that speaks MCP can shop here. The tools are a thin,
typed shell over :class:`merchant.service.MerchantService` — the shell does
argument marshalling and the service does the work, so the same code path is
exercised whether a request arrives over MCP, over HTTP, or from a test.

The tool surface is deliberately small and deliberately shaped. Note what is
*not* here: no tool to adjust a price, no tool to skip verification, no tool to
retry a payment, no tool to grant authority. An agent connected to this server
can look at a catalogue and present mandates. That is the whole of its power, and
it is enforced by the absence of a function rather than by a check inside one.

Tools run with ``run_in_thread=False``. They are microseconds of in-process work,
and running them on the event loop keeps every SQLite handle on one thread.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from gateway.bootstrap import Gateway, build_gateway
from gateway.config import ConfigurationError, load_dotenv

INSTRUCTIONS = """
An AP2 v0.2 Merchant and Merchant Payment Processor, backed by Razorpay test mode.

The purchase lifecycle, in order:

  1. search_inventory / check_product / check_serviceability — browse.
  2. assemble_cart(items)                — price a single-merchant basket.
  3. create_checkout(cart_id)            — returns a MERCHANT-SIGNED Checkout
                                           Mandate guaranteeing that price for a
                                           short window, plus the open Checkout
                                           Mandate your buyer's authorisation
                                           must satisfy.
  4. complete_checkout(checkout_id, checkout_mandate_jwt)
                                         — present the buyer's OPEN Checkout
                                           Mandate. Returns a Checkout Receipt,
                                           or an `unresolved_constraint` error if
                                           the basket is outside it.
  5. initiate_payment(checkout_id, payment_mandate_jwt)
                                         — present an agent-signed CLOSED Payment
                                           Mandate (vct mandate.payment.1) that
                                           embeds the buyer's open Payment
                                           Mandate. Returns a Payment Receipt, an
                                           `unresolved_constraint` with an
                                           approval_url for a human, or a
                                           structured denial.

Every mandate is verified in deterministic code. If you are outside your standing
authorisation, present the OPEN mandate rather than forcing a closed one: you
will get `unresolved_constraint` and an approval_url, which is the supported path.
Forcing an over-limit closed mandate is denied.
"""


def build_server(gateway: Gateway | None = None) -> tuple[FastMCP, Gateway]:
    """Build the MCP server over a gateway. Returns both, for in-process clients."""
    wired = gateway or build_gateway(
        db_path=os.environ.get("GATEWAY_DB"), rail_kind=os.environ.get("PAYMENT_RAIL")
    )
    merchant = wired.merchant
    mcp: FastMCP = FastMCP(name="ap2-razorpay-merchant", instructions=INSTRUCTIONS)

    @mcp.tool(run_in_thread=False)
    def search_inventory(
        query: Annotated[str, Field(description="Free-text search over name, category and SKU.")],
        filters: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Optional: max_price_inr, min_price_inr, category, merchant_id, size, "
                    "pincode, in_stock_only, limit."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Search the catalogue. Returns products with live stock and prices in paise."""
        return merchant.search_inventory(query, filters)

    @mcp.tool(run_in_thread=False)
    def check_product(
        # Named `id` because the AP2 tool contract in the brief names it `id`;
        # shadowing the builtin inside this one signature is the lesser evil.
        id: Annotated[str, Field(description="The SKU, e.g. SF-RUN-001.")],
    ) -> dict[str, Any]:
        """Look up one SKU. Returns `{"error": "product.not_found"}` if it does not exist."""
        return merchant.check_product(id)

    @mcp.tool(run_in_thread=False)
    def check_serviceability(
        pincode: Annotated[str, Field(description="Indian PIN code, e.g. 560001.")],
    ) -> dict[str, Any]:
        """Which merchants deliver to this pincode."""
        return merchant.check_serviceability(pincode)

    @mcp.tool(run_in_thread=False)
    def assemble_cart(
        items: Annotated[
            list[dict[str, Any]],
            Field(description='Lines as [{"sku": "SF-RUN-001", "qty": 1}]. One merchant only.'),
        ],
        ship_to_pincode: Annotated[
            str | None, Field(description="Delivery pincode. Defaults to the policy pincode.")
        ] = None,
    ) -> dict[str, Any]:
        """Price a basket and stamp the prices in. Validates every SKU and stock level."""
        return merchant.assemble_cart(items, ship_to_pincode)

    @mcp.tool(run_in_thread=False)
    def create_checkout(
        cart_id: Annotated[str, Field(description="From assemble_cart.")],
    ) -> dict[str, Any]:
        """Sign the cart: a merchant-signed Checkout Mandate (ES256), plus the open
        Checkout Mandate template your buyer's standing authorisation must satisfy."""
        return merchant.create_checkout(cart_id)

    @mcp.tool(run_in_thread=False)
    def complete_checkout(
        checkout_id: Annotated[str, Field(description="From create_checkout.")],
        checkout_mandate_jwt: Annotated[
            str,
            Field(
                description=(
                    "The buyer's user-signed Checkout Mandate as a compact JWS — the OPEN "
                    "standing authorisation, or a CLOSED confirmation after human approval."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Check the buyer's Checkout Mandate against this basket.

        Returns a Checkout Receipt, or an `unresolved_constraint` error naming the
        constraint that a human needs to resolve.
        """
        return merchant.complete_checkout(checkout_id, checkout_mandate_jwt)

    @mcp.tool(run_in_thread=False)
    def initiate_payment(
        checkout_id: Annotated[str, Field(description="A confirmed checkout.")],
        payment_mandate_jwt: Annotated[
            str,
            Field(
                description=(
                    "An agent-signed CLOSED Payment Mandate (vct mandate.payment.1) embedding "
                    "the buyer's open Payment Mandate. Present the OPEN mandate instead if you "
                    "know you are outside your standing authorisation."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Run the deterministic verifier and, on ALLOW, pay with bounded recovery.

        Returns a signed Payment Receipt (captured or failed), an
        `unresolved_constraint` with an approval_url, a structured denial, or a
        `deferred` status when the payment rail is unreachable and the mandate has
        deliberately been left unspent.
        """
        return merchant.initiate_payment(checkout_id, payment_mandate_jwt)

    return mcp, wired


def main() -> None:
    """Run the Merchant MCP server on stdio. `make mcp`."""
    import sys

    load_dotenv()
    try:
        server, wired = build_server()
    except ConfigurationError as exc:
        print(f"\n  Cannot start: {exc}\n", file=sys.stderr)
        raise SystemExit(2) from None
    print(
        f"  merchant MCP server ready — {len(wired.catalog.products)} SKUs, "
        f"{len(wired.catalog.merchants)} merchants, {wired.rail.name} rail"
    )
    server.run()


if __name__ == "__main__":
    main()
