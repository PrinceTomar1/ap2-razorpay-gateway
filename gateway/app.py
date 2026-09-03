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
from gateway.config import ConfigurationError, load_dotenv
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


#: Env-gated demo seeding. Off by default, so `make serve` locally is unchanged.
#: A hosted demo that greets a reviewer with "nothing waiting on you" wastes the
#: one click they were willing to give it.
SEED_ENV_VAR = "DEMO_SEED_HOLD"

#: The ₹4,999 racing shoe — attempt 3 of the batch, and the only one that reaches
#: a human. Seeding it is safe: raising a hold grants no authority whatsoever.
#: Only a human decision on the page mints a mandate, and even then it is scoped
#: to one amount at one merchant for one basket for ten minutes.
SEED_SKU = "SF-RUN-004"


def seed_demo_hold(gateway: Gateway) -> str | None:
    """Raise one pending Trusted Surface hold, so the live page shows something.

    Returns the hold id, or None if the catalogue could not produce one. Never
    raises: a demo convenience must not be able to stop the service booting.
    """
    try:
        cart = gateway.merchant.assemble_cart([{"sku": SEED_SKU, "qty": 1}])["cart"]
        checkout = gateway.merchant.create_checkout(cart["cart_id"])
        response = gateway.merchant.complete_checkout(
            checkout["checkout_id"], gateway.open_checkout_jws
        )
    except Exception:  # noqa: BLE001 — seeding is decoration; booting is not
        return None
    hold_id = response.get("hold_id")
    return str(hold_id) if hold_id else None


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

    if os.environ.get(SEED_ENV_VAR) == "1":
        seed_demo_hold(wired)

    app.include_router(build_trusted_surface_router(wired.trusted_surface))
    app.include_router(build_webhook_router(WebhookReceiver(audit=wired.audit)))

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        pending = wired.trusted_surface.pending()
        if not pending and os.environ.get(SEED_ENV_VAR) == "1":
            # Somebody decided the last one. Raise a fresh hold so the next
            # visitor sees the gate rather than an empty list.
            seed_demo_hold(wired)
            pending = wired.trusted_surface.pending()
        rows = (
            "".join(
                f'<li><a href="/trusted-surface/{h.hold_id}">₹{paise_to_inr_str(h.amount)} '
                f"at {h.cart.merchant_name}</a> — over the buyer's ₹1,500 limit</li>"
                for h in pending
            )
            or "<li>nothing waiting on you</li>"
        )
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>AP2 × Razorpay gateway</title>"
            "<style>body{font:15px/1.6 system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem}"
            "code{background:#f4f4f4;padding:.1rem .3rem;border-radius:3px}</style>"
            "<h1>AP2 × Razorpay gateway</h1>"
            "<p>An implementation of Google's <b>Agent Payments Protocol (AP2 v0.2)</b> "
            "for Razorpay. A shopping agent can buy from a merchant here, and every "
            "money action is verified in deterministic code before a rupee moves.</p>"
            "<p><b>Test rail only.</b> No Razorpay credentials exist in this "
            "environment and no live path exists in the code, so nothing here can "
            "move real money.</p>"
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
            "<h2>Source</h2>"
            "<p><a href='https://github.com/PrinceTomar1/ap2-razorpay-gateway'>"
            "github.com/PrinceTomar1/ap2-razorpay-gateway</a> — "
            "<code>make demo</code> runs the full six-attempt batch offline.</p>"
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
    import sys

    import uvicorn

    load_dotenv()
    # $PORT is what every PaaS injects (Railway, Render, Fly, Heroku). It wins,
    # because a host that tells you which port to bind is not making a suggestion.
    # $GATEWAY_HOST defaults to loopback locally but must be 0.0.0.0 in a
    # container, or the platform's health check can never reach us.
    port = int(os.environ.get("PORT") or os.environ.get("GATEWAY_PORT") or "8000")
    host = os.environ.get("GATEWAY_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    try:
        app = create_app()
    except ConfigurationError as exc:
        print(f"\n  Cannot start: {exc}\n", file=sys.stderr)
        raise SystemExit(2) from None
    print(f"  gateway on http://{host}:{port}  (Trusted Surface + Razorpay webhooks)")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
