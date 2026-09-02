# ---------------------------------------------------------------------------
# ap2-razorpay-gateway
#
#   make setup   create .venv and install pinned deps; seed .env from .env.example
#   make test    the full pytest suite (offline, FakeRail)
#   make lint    ruff + mypy, both must be clean
#   make demo    the 6-attempt batch against FakeRail, zero network
#   make demo LIVE=1
#                attempts 1 and 4 against the real Razorpay TEST sandbox
#   make mcp     run the Merchant MCP server on stdio
#   make serve   run the gateway (Razorpay webhooks + Trusted Surface page)
#   make smoke   fresh-clone smoke test in a temp dir
# ---------------------------------------------------------------------------

PY      := .venv/bin/python
PIP     := .venv/bin/pip
VENV    := .venv

.DEFAULT_GOAL := help
.PHONY: help setup test lint fmt demo mcp serve smoke clean

help:
	@grep -E '^#   make' Makefile | sed 's/^#   //'

$(VENV)/pyvenv.cfg:
	python3 -m venv $(VENV)

setup: $(VENV)/pyvenv.cfg
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -r requirements.txt
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")
	@mkdir -p run
	@echo "setup complete — $$($(PY) --version)"

test:
	@$(PY) -m pytest

lint:
	@echo "--- ruff ---"
	@$(PY) -m ruff check .
	@$(PY) -m ruff format --check .
	@echo "--- mypy ---"
	@$(PY) -m mypy
	@echo "lint clean"

fmt:
	@$(PY) -m ruff format .
	@$(PY) -m ruff check --fix .

# LIVE=1 switches demo/batch.py onto the real Razorpay test-mode rail for
# attempts 1 and 4 only. Without it, everything runs against FakeRail offline.
demo:
ifeq ($(LIVE),1)
	@$(PY) -m demo.batch --live
else
	@$(PY) -m demo.batch
endif

mcp:
	@$(PY) -m merchant.mcp_server

serve:
	@$(PY) -m gateway.app

smoke:
	@bash scripts/smoke.sh

clean:
	@rm -rf .pytest_cache .mypy_cache .ruff_cache run demo/report.json
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned"
