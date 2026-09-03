"""The gateway service: one FastAPI app, two things worth exposing over HTTP.

    /trusted-surface/{hold_id}   the human approval page
    /webhooks/razorpay           Razorpay payment callbacks

Everything else — the catalogue, checkout, the verifier, payment — is reached
over MCP (``make mcp``), because that is the interface an AP2 shopping agent
speaks. This service exists for the two parties that are *not* agents: a person
with a browser, and Razorpay with an HTTP client.

``make demo`` does not need this running. The whole batch, including the human
gate, executes in-process.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from ap2_min.models import paise_to_inr_str
from gateway.bootstrap import Gateway, build_gateway
from gateway.trusted_surface import build_router as build_trusted_surface_router
from gateway.webhooks import WebhookReceiver
from gateway.webhooks import build_router as build_webhook_router

DESCRIPTION = """
An implementation of Google's Agent Payments Protocol (AP2 v0.2) for Razorpay.

Implements the AP2 **Merchant** and **Merchant Payment Processor** roles so any
AP2 shopping agent can buy from an Indian merchant with cryptographically
verifiable mandates, deterministic verification, a human gate, and a
tamper-evident audit trail.

Test mode only. This service will refuse a live Razorpay key.
"""


def create_app(gateway: Gateway | None = None) -> FastAPI:
    wired = gateway or build_gateway(
        db_path=os.environ.get("GATEWAY_DB"), rail_kind=os.environ.get("PAYMENT_RAIL")
    )
    app = FastAPI(
        title="ap2-razorpay-gateway",
        version="0.2.0",
        description=DESCRIPTION,
    )
    app.state.gateway = wired

    app.include_router(build_trusted_surface_router(wired.trusted_surface))
    app.include_router(build_webhook_router(WebhookReceiver(audit=wired.audit)))

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        pending = wired.trusted_surface.pending()
        rows = (
            "".join(
                f'<li><a href="/trusted-surface/{h.hold_id}">₹{paise_to_inr_str(h.amount)} '
                f"at {h.cart.merchant_name}</a></li>"
                for h in pending
            )
            or "<li>nothing waiting on you</li>"
        )
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>AP2 × Razorpay gateway</title>"
            "<style>body{font:15px/1.6 system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem}"
            "code{background:#f4f4f4;padding:.1rem .3rem;border-radius:3px}</style>"
            "<h1>AP2 × Razorpay gateway</h1>"
            f"<p>Rail: <code>{wired.rail.name}</code> · "
            f"{len(wired.catalog.products)} SKUs · "
            f"{wired.audit.count()} audit rows</p>"
            f"<h2>Awaiting your approval</h2><ul>{rows}</ul>"
            "<h2>Elsewhere</h2><ul>"
            "<li><code>POST /webhooks/razorpay</code> — Razorpay callbacks</li>"
            "<li><code>GET /health</code></li>"
            "<li><code>GET /audit</code> — the chain, and whether it verifies</li>"
            "<li>The merchant itself is an MCP server: <code>make mcp</code></li>"
            "</ul>"
        )

    @app.get("/health")
    def health() -> JSONResponse:
        chain = wired.audit.verify_chain()
        return JSONResponse(
            {
                "status": "ok" if chain.ok else "audit_chain_broken",
                "rail": wired.rail.name,
                "narration": wired.llm.name if wired.llm else "templates",
                "catalogue_skus": len(wired.catalog.products),
                "audit_rows": chain.rows_checked,
                "audit_chain_intact": chain.ok,
            }
        )

    @app.get("/audit")
    def audit_trail(limit: int = 200) -> JSONResponse:
        """The audit chain, plus the verification result.

        Read-only, and it publishes the tip hash: anyone who records the tip today
        can detect truncation tomorrow, which is the one tamper a self-contained
        hash chain cannot catch on its own. See LIMITATIONS.md.
        """
        chain = wired.audit.verify_chain()
        rows: list[dict[str, Any]] = [
            {
                "id": row.id,
                "ts": row.ts,
                "actor": row.actor,
                "event": row.event,
                "human_reason": row.human_reason,
                "payload": row.payload,
                "hash": row.hash,
                "prev_hash": row.prev_hash,
            }
            for row in wired.audit.rows(limit=limit)
        ]
        return JSONResponse(
            {
                "verified": chain.ok,
                "rows_checked": chain.rows_checked,
                "broken_at": chain.broken_at,
                "reason": chain.reason,
                "tip_hash": wired.audit.tip_hash(),
                "rows": rows,
            }
        )

    return app


def main() -> None:
    """`make serve`."""
    import uvicorn

    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8000"))
    print(f"  gateway on http://{host}:{port}  (Trusted Surface + Razorpay webhooks)")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
