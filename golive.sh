#!/usr/bin/env bash
# Usage:  ./golive.sh rzp_test_XXXXXXXX YOUR_SECRET_HERE
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: ./golive.sh <key_id> <key_secret>"; exit 1; }
KEY_ID="$1"; KEY_SECRET="$2"

python3 - "$KEY_ID" "$KEY_SECRET" <<'PY'
import pathlib, re, sys
kid, sec = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); t = p.read_text()
def setkv(t, k, v):
    return re.sub(rf"^{k}=.*$", f"{k}={v}", t, flags=re.M) if re.search(rf"^{k}=", t, re.M) else t + f"\n{k}={v}\n"
t = setkv(t, "RAZORPAY_KEY_ID", kid)
t = setkv(t, "RAZORPAY_KEY_SECRET", sec)
t = setkv(t, "PAYMENT_RAIL", "razorpay")
p.write_text(t)
print("  .env updated — PAYMENT_RAIL=razorpay")
PY

echo "  verifying the credentials against Razorpay before the demo..."
.venv/bin/python - <<'PY'
import os, sys, razorpay
sys.path.insert(0, ".")
from gateway.config import load_dotenv
load_dotenv()
c = razorpay.Client(auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"]))
try:
    o = c.order.create({"amount": 100, "currency": "INR", "payment_capture": 0})
    print(f"  ✓ credentials work — test order {o['id']} created")
except Exception as e:
    print(f"  ✗ credentials rejected: {e}")
    raise SystemExit(1)
PY

echo "  running the live demo..."
make demo LIVE=1
