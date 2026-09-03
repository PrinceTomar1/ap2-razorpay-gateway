"""The agent's view of the merchant: seven MCP tool calls.

Thin on purpose. It unwraps FastMCP's ``CallToolResult`` into the plain dict the
agent reasons about, and does nothing else — no retries, no caching, no
interpretation. Anything cleverer here would be logic the merchant cannot see and
the audit log cannot record.

Using a real MCP client rather than calling
:class:`~merchant.service.MerchantService` directly matters: it means the demo
exercises the actual tool schemas, the actual argument validation and the actual
serialisation boundary that a third-party AP2 agent would hit. With FastMCP's
in-process transport that costs no network and no subprocess.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Client


class McpMerchantTools:
    """Calls the Merchant MCP server. Satisfies :class:`shopping_agent.agent.MerchantTools`."""

    def __init__(self, client: Client[Any]) -> None:
        self.client = client
        #: Every tool call made, in order. The demo prints this and tests assert
        #: on it — including asserting that some calls never happened.
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, arguments))
        result = await self.client.call_tool(tool, arguments)
        data = result.data
        if not isinstance(data, dict):  # pragma: no cover — every tool returns an object
            raise TypeError(f"{tool} returned {type(data).__name__}, expected a JSON object")
        return data

    async def search_inventory(
        self, query: str, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._call("search_inventory", {"query": query, "filters": filters})

    async def check_product(self, product_id: str) -> dict[str, Any]:
        return await self._call("check_product", {"id": product_id})

    async def check_serviceability(self, pincode: str) -> dict[str, Any]:
        return await self._call("check_serviceability", {"pincode": pincode})

    async def assemble_cart(
        self, items: list[dict[str, Any]], ship_to_pincode: str | None = None
    ) -> dict[str, Any]:
        return await self._call(
            "assemble_cart", {"items": items, "ship_to_pincode": ship_to_pincode}
        )

    async def create_checkout(self, cart_id: str) -> dict[str, Any]:
        return await self._call("create_checkout", {"cart_id": cart_id})

    async def complete_checkout(
        self, checkout_id: str, checkout_mandate_jwt: str
    ) -> dict[str, Any]:
        return await self._call(
            "complete_checkout",
            {"checkout_id": checkout_id, "checkout_mandate_jwt": checkout_mandate_jwt},
        )

    async def initiate_payment(self, checkout_id: str, payment_mandate_jwt: str) -> dict[str, Any]:
        return await self._call(
            "initiate_payment",
            {"checkout_id": checkout_id, "payment_mandate_jwt": payment_mandate_jwt},
        )
