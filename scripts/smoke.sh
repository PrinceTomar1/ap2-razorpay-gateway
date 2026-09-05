#!/usr/bin/env bash
#
# Fresh-clone smoke test.
#
# Copies the repository to a throwaway directory with nothing carried over — no
# .venv, no .env, no database, no caches — then does exactly what a reviewer
# would do on a fresh clone:
#
#     cp .env.example .env && make setup && make demo
#
# Passes only if the demo prints the exact expected report line. The point is to
# catch the class of bug where a project works on the machine it was built on and
# nowhere else: a file that was never committed, a path assumption, a leftover
# database, an environment variable that happened to be exported.
#
set -euo pipefail

EXPECTED="6 attempts · 4 paid · 1 human-denied · 1 recovered · ₹0 unauthorised · 6/6 explained"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "→ source        $SOURCE"
echo "→ temp clone    $TMP/ap2-razorpay-gateway"

# Copy tracked files only, exactly as a git clone would deliver them.
if git -C "$SOURCE" rev-parse --git-dir >/dev/null 2>&1; then
  mkdir -p "$TMP/ap2-razorpay-gateway"
  git -C "$SOURCE" archive HEAD | tar -x -C "$TMP/ap2-razorpay-gateway"
  echo "→ copied via    git archive HEAD (tracked files only)"
else
  rsync -a --exclude .venv --exclude .git --exclude '__pycache__' \
        --exclude '*.db' --exclude .env --exclude run \
        "$SOURCE/" "$TMP/ap2-razorpay-gateway/"
  echo "→ copied via    rsync"
fi

cd "$TMP/ap2-razorpay-gateway"

# Prove nothing came along that should not have.
for forbidden in .venv .env run demo/report.json; do
  if [ -e "$forbidden" ]; then
    echo "✗ the clone contains $forbidden, which must never be committed" >&2
    exit 1
  fi
done
echo "→ clean         no .venv, no .env, no database, no report"

# Prove no environment is leaking in from the parent shell.
unset PAYMENT_RAIL RAZORPAY_KEY_ID RAZORPAY_KEY_SECRET RAZORPAY_WEBHOOK_SECRET
unset ANTHROPIC_API_KEY LLM_PROVIDER GATEWAY_DB POLICY_FILE

echo
echo "→ cp .env.example .env"
cp .env.example .env

echo "→ make setup"
make setup

echo "→ make demo"
OUTPUT="$(make demo)"
echo "$OUTPUT"

# Strip ANSI colour before comparing.
PLAIN="$(printf '%s' "$OUTPUT" | sed -e 's/\x1b\[[0-9;]*m//g')"

echo
if printf '%s' "$PLAIN" | grep -qF "$EXPECTED"; then
  echo "✓ fresh clone produced the expected report line:"
  echo "  $EXPECTED"
else
  echo "✗ expected line not found:" >&2
  echo "  $EXPECTED" >&2
  exit 1
fi

if [ -f demo/report.json ]; then
  echo "✓ demo/report.json written"
else
  echo "✗ demo/report.json missing" >&2
  exit 1
fi

echo "✓ fresh-clone smoke test passed"
echo "  (make setup fetches pinned wheels from PyPI; make demo itself opens no sockets —"
echo "   proved by tests/test_demo.py::test_the_batch_opens_no_sockets)"
