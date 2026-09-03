"""The MCP surface.

An AP2 agent's entire view of this merchant is these seven tools, so the tests
here are as much about the *shape* of the surface as about what it returns —
including that the tools an agent might wish existed do not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastmcp import Client

from ap2_min.models import inr
from gateway.bootstrap import Gateway
from gateway.razorpay_client import FakeRail
from merchant.mcp_server import build_server
from shopping_agent.mcp_tools import McpMerchantTools

EXPECTED_TOOLS = {
    "search_inventory",
    "check_product",
    "check_serviceability",
    "assemble_cart",
    "create_checkout",
    "complete_checkout",
    "initiate_payment",
}


@pytest_asyncio.fixture
async def tools(wired: Gateway) -> AsyncIterator[McpMerchantTools]:
    """A real MCP client over an in-process transport. Real protocol, no network."""
    server, _ = build_server(wired)
    async with Client(server) as client:
        yield McpMerchantTools(client)


@pytest.mark.asyncio
async def test_the_server_exposes_exactly_seven_tools(wired: Gateway) -> None:
    server, _ = build_server(wired)
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_there_is_no_tool_that_grants_authority(wired: Gateway) -> None:
    """The bound is the absence of a function, not a check inside one.

    No tool adjusts a price, skips verification, retries a payment, approves a
    hold or raises a limit. An agent connected here can browse and present
    mandates. That is all it can do.
    """
    server, _ = build_server(wired)
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    forbidden = {
        "set_price",
        "update_price",
        "approve",
        "approve_hold",
        "skip_verification",
        "override_limit",
        "raise_limit",
        "retry_payment",
        "refund",
        "capture",
        "create_mandate",
        "sign_mandate",
    }
    assert names & forbidden == set()


@pytest.mark.asyncio
async def test_every_tool_documents_itself(wired: Gateway) -> None:
    server, _ = build_server(wired)
    async with Client(server) as client:
        for tool in await client.list_tools():
            assert tool.description, f"{tool.name} has no description"
            assert tool.inputSchema


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_over_mcp(tools: McpMerchantTools) -> None:
    response = await tools.search_inventory("running", {"max_price_inr": 1500})
    assert response["count"] > 0
    assert all(p["price_paise"] <= inr(1500) for p in response["results"])


@pytest.mark.asyncio
async def test_check_product_over_mcp(tools: McpMerchantTools) -> None:
    found = await tools.check_product("SF-RUN-001")
    assert found["found"] is True
    assert found["product"]["price_paise"] == inr(1299)

    missing = await tools.check_product("SF-RUN-999")
    assert missing["error"] == "product.not_found"


@pytest.mark.asyncio
async def test_serviceability_over_mcp(tools: McpMerchantTools) -> None:
    response = await tools.check_serviceability("560001")
    assert response["serviceable"] is True


@pytest.mark.asyncio
async def test_the_full_purchase_lifecycle_over_mcp(
    tools: McpMerchantTools, wired: Gateway
) -> None:
    cart = (await tools.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], "560001"))["cart"]
    checkout = await tools.create_checkout(cart["cart_id"])
    confirmed = await tools.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)
    assert confirmed["status"] == "confirmed"

    from ap2_min.builders import closed_payment_mandate
    from gateway.mandates import utcnow

    now = utcnow()
    mandate = closed_payment_mandate(
        payee=cart["merchant_id"],
        payee_name=cart["merchant_name"],
        amount=cart["total"],
        payment_instrument="upi",
        checkout_hash=checkout["checkout_hash"],
        open_mandate_jws=wired.open_payment_jws,
        execution_date=now,
    )
    paid = await tools.initiate_payment(
        checkout["checkout_id"], wired.agent.sign(mandate, ttl_seconds=600, now=now)
    )
    assert paid["status"] == "captured"
    assert paid["payment_receipt_jws"]

    assert [name for name, _ in tools.calls] == [
        "assemble_cart",
        "create_checkout",
        "complete_checkout",
        "initiate_payment",
    ]


@pytest.mark.asyncio
async def test_a_garbage_mandate_over_mcp_is_a_typed_error_not_a_crash(
    tools: McpMerchantTools, wired: Gateway
) -> None:
    cart = (await tools.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], "560001"))["cart"]
    checkout = await tools.create_checkout(cart["cart_id"])
    await tools.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)

    response = await tools.initiate_payment(checkout["checkout_id"], "not-a-jwt")
    assert response["error"] == "mandate.malformed"
    assert isinstance(wired.rail, FakeRail)
    assert wired.rail.captured_total() == 0


@pytest.mark.asyncio
async def test_tool_arguments_are_schema_validated(wired: Gateway) -> None:
    """A wrong-typed argument is refused by the transport, before our code runs."""
    server, _ = build_server(wired)
    async with Client(server) as client:
        with pytest.raises(Exception, match=r"(?i)valid|error"):
            await client.call_tool("check_product", {"id": {"not": "a string"}})


@pytest.mark.asyncio
async def test_the_server_carries_usage_instructions(wired: Gateway) -> None:
    """An agent that has never seen this merchant should be able to work it out."""
    server, _ = build_server(wired)
    assert server.instructions is not None
    for expected in ("mandate.payment.1", "unresolved_constraint", "complete_checkout"):
        assert expected in server.instructions


# ---------------------------------------------------------------------------
# Error paths — every tool, over a real MCP client
#
# A tool that only works when everything is right is a tool nobody can build an
# agent against. Each of these asserts a stable machine `error` code, because
# that is what a shopping agent branches on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_with_no_matches_returns_an_empty_result_not_an_error(
    tools: McpMerchantTools,
) -> None:
    """Nothing found is a valid answer, not a failure."""
    response = await tools.search_inventory("submarine parts", {"max_price_inr": 1})
    assert response["count"] == 0
    assert response["results"] == []
    assert "error" not in response


@pytest.mark.asyncio
async def test_search_filters_out_of_stock_items(tools: McpMerchantTools, wired: Gateway) -> None:
    wired.catalog.set_stock("SF-RUN-001", 0)
    response = await tools.search_inventory("velocity", None)
    assert "SF-RUN-001" not in [p["sku"] for p in response["results"]]


@pytest.mark.asyncio
async def test_serviceability_error_path(tools: McpMerchantTools) -> None:
    response = await tools.check_serviceability("999999")
    assert response["serviceable"] is False
    assert response["merchants"] == []


@pytest.mark.asyncio
async def test_assemble_cart_rejects_a_hallucinated_sku(tools: McpMerchantTools) -> None:
    response = await tools.assemble_cart([{"sku": "NOPE-999", "qty": 1}], "560001")
    assert response["error"] == "product.not_found"
    assert response["sku"] == "NOPE-999"


@pytest.mark.asyncio
async def test_assemble_cart_rejects_a_two_merchant_basket(tools: McpMerchantTools) -> None:
    response = await tools.assemble_cart(
        [{"sku": "SF-RUN-001", "qty": 1}, {"sku": "LM-KIT-002", "qty": 1}], "560001"
    )
    assert response["error"] == "cart.mixed_merchants"
    assert sorted(response["merchants"]) == ["m_lumen", "m_stridefit"]


@pytest.mark.asyncio
async def test_assemble_cart_rejects_an_unserviceable_pincode(
    tools: McpMerchantTools,
) -> None:
    response = await tools.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], "999999")
    assert response["error"] == "pincode.not_serviceable"


@pytest.mark.asyncio
async def test_assemble_cart_rejects_more_than_the_shelf_holds(
    tools: McpMerchantTools,
) -> None:
    response = await tools.assemble_cart([{"sku": "SF-RUN-004", "qty": 500}], "560001")
    assert response["error"] == "product.out_of_stock"


@pytest.mark.asyncio
async def test_create_checkout_rejects_an_unknown_cart(tools: McpMerchantTools) -> None:
    response = await tools.create_checkout("cart_never_existed")
    assert response["error"] == "catalog.error"
    assert "cart_never_existed" in response["message"]


@pytest.mark.asyncio
async def test_complete_checkout_rejects_a_merchant_signed_authorisation(
    tools: McpMerchantTools, wired: Gateway
) -> None:
    """Only the buyer may sign a standing authorisation. A shop may not."""
    cart = (await tools.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], "560001"))["cart"]
    checkout = await tools.create_checkout(cart["cart_id"])
    forged = wired.merchant_signer.sign(wired.open_checkout_contents, ttl_seconds=600)

    response = await tools.complete_checkout(checkout["checkout_id"], forged)
    assert response["error"] == "mandate.wrong_issuer"


@pytest.mark.asyncio
async def test_complete_checkout_returns_unresolved_constraint_over_the_cap(
    tools: McpMerchantTools, wired: Gateway
) -> None:
    """The AP2 error shape an agent must be able to act on, delivered over MCP."""
    cart = (await tools.assemble_cart([{"sku": "SF-RUN-004", "qty": 1}], "560001"))["cart"]
    checkout = await tools.create_checkout(cart["cart_id"])

    response = await tools.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)

    assert response["error"] == "unresolved_constraint"
    assert response["constraint"] == "checkout.amount_exceeds_standing_limit"
    assert response["hold_id"]
    assert response["approval_url"].endswith(response["hold_id"])
    assert response["amount"] == inr(4999)
    assert response["human_reason"]


@pytest.mark.asyncio
async def test_complete_checkout_rejects_an_unknown_checkout(
    tools: McpMerchantTools, wired: Gateway
) -> None:
    response = await tools.complete_checkout("chk_nope", wired.open_checkout_jws)
    assert response["error"] == "catalog.error"


@pytest.mark.asyncio
async def test_initiate_payment_refuses_an_unconfirmed_checkout(
    tools: McpMerchantTools, wired: Gateway
) -> None:
    """Order of operations, enforced over the wire."""
    cart = (await tools.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], "560001"))["cart"]
    checkout = await tools.create_checkout(cart["cart_id"])

    response = await tools.initiate_payment(checkout["checkout_id"], "anything-at-all")
    assert response["error"] == "checkout.not_confirmed"
    assert isinstance(wired.rail, FakeRail)
    assert wired.rail.calls == []


@pytest.mark.asyncio
async def test_initiate_payment_refuses_an_unknown_checkout(tools: McpMerchantTools) -> None:
    response = await tools.initiate_payment("chk_nope", "anything")
    assert response["error"] == "catalog.error"


@pytest.mark.asyncio
async def test_every_error_response_carries_a_stable_machine_code(
    tools: McpMerchantTools,
) -> None:
    """An agent branches on `error`. Every failure path must supply one."""
    responses = [
        await tools.check_product("NOPE"),
        await tools.assemble_cart([{"sku": "NOPE", "qty": 1}], "560001"),
        await tools.create_checkout("cart_nope"),
        await tools.initiate_payment("chk_nope", "x"),
    ]
    for response in responses:
        assert isinstance(response.get("error"), str) and response["error"]
        assert "." in response["error"], "codes are namespaced, e.g. product.not_found"
