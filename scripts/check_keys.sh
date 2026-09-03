#!/usr/bin/env bash
#
# Validate Razorpay test keys in ~30 seconds, before committing to a full
# `make demo LIVE=1` run.
#
# Creates one ₹1 order against the real sandbox and deletes nothing — a
# never-paid order costs nothing and expires on its own. If this prints OK, the
# live path works and `make demo LIVE=1` will too.
#
#   ./scripts/check_keys.sh
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -f .env ]; then
  echo "✗ no .env — run: cp .env.example .env" >&2
  exit 1
fi

.venv/bin/python - <<'PY'
import sys

from gateway.config import ConfigurationError, load_dotenv

load_dotenv()

import os

key_id = os.environ.get("RAZORPAY_KEY_ID", "")
key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")

if not key_id or key_id.startswith("rzp_test_x") or not key_secret:
    print(
        "\n  Your .env still has the placeholder keys.\n"
        "  Get real TEST-mode keys here (flip the toggle to Test Mode first):\n"
        "      https://dashboard.razorpay.com/app/website-app-settings/api-keys\n"
        "  then put them in .env as RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET.\n",
        file=sys.stderr,
    )
    raise SystemExit(1)

from gateway.razorpay_client import RazorpayRail

try:
    rail = RazorpayRail(key_id, key_secret)
except ConfigurationError as exc:
    print(f"\n  {exc}\n", file=sys.stderr)
    raise SystemExit(2) from None

print(f"  key id      {key_id[:14]}…  (test mode confirmed)")
print("  creating a Rs 1 order against the real sandbox…")

try:
    order = rail.create_order(
        amount=100,  # Rs 1, in paise
        currency="INR",
        receipt="ap2-key-check",
        notes={"purpose": "credential check", "protocol": "ap2-v0.2"},
    )
except Exception as exc:  # noqa: BLE001 — surface whatever Razorpay said, verbatim
    print(f"\n✗ Razorpay refused the request:\n    {exc}\n", file=sys.stderr)
    print(
        "  Most common cause: key id and secret from different keypairs, or a\n"
        "  secret copied with a trailing space. Regenerate and try again.\n",
        file=sys.stderr,
    )
    raise SystemExit(3) from None

print(f"  order       {order.id}  Rs {order.amount / 100:.2f}  status={order.status}")
print("\n  OK — your keys work. `make demo LIVE=1` will run.")
print("  (That Rs 1 order was never paid; it costs nothing and expires by itself.)")
PY
