# Working proof

Every item in the zero-error gate, with the real output. Run on a clean tree with
`.venv`, all caches and the database deleted first.

**Nothing here is transcribed from memory.** Each block is the output of the
command above it, captured in the same session that produced this file.

---

## ☑ `make setup && make test`

```
$ rm -rf .venv .mypy_cache .pytest_cache .ruff_cache run
$ make setup
setup complete — Python 3.13.5

$ make test
542 passed in 6.28s
```

**542 tests. 0 skipped. 0 xfail.** `addopts` carries `--strict-markers` and no
`-k` filter — the full suite runs every time.

| File | Tests | Covers |
|---|---|---|
| `test_docs.py` | 59 | Documentation must agree with the code |
| `test_mandates.py` | 53 | JWS envelope, `alg:none`, HMAC/EC confusion, model invariants |
| `test_correctness.py` | 51 | The adversarial "what input breaks this?" pass |
| `test_security.py` | 39 | Signature bypass, replay, secrets, the Trusted Surface |
| `test_verify.py` | 40 | Every check, every boundary, determinism, purity |
| `test_failure_modes.py` | 34 | All eight failure modes + the no-LLM grep |
| `test_merchant.py` | 27 | Catalogue, carts, the stock/price re-check |
| `test_recovery.py` | 26 | Instrument ladder, breaker, backoff, exhaustion |
| `test_ap2_fidelity.py` | 26 | Every spec value, as a table the build enforces |
| `test_mcp_tools.py` | 24 | Seven tools, happy **and** error path each |
| `test_config.py` | 24 | `.env` parsing, the live-key guard |
| `test_standout.py` | 23 | Red team, benchmark, interop and timeline as gates |
| `test_idempotency.py` | 23 | Duplicate, concurrent, retry-after-decline/timeout |
| `test_audit_chain.py` | 23 | Tamper detection |
| `test_demo.py` | 21 | The report line, and that it is measured |
| `test_trusted_surface.py` | 18 | The gate, and that approval is not an unlock |
| `test_payments.py` | 18 | The ALLOW gate, receipts, budget accounting |
| `test_receipts.py` | 10 | External verifiability, three-way reconciliation |

## ☑ `ruff check .`

```
$ ruff check .
All checks passed!
```

`E,W,F,I,UP,B,SIM,C4,BLE,RUF`. `BLE` is on deliberately, so every blind
`except Exception` needs a justified `noqa` — there are three, all on paths where
a failure must not propagate (narration ×2, red-team harness ×1).

## ☑ `mypy --strict`

```
$ mypy --strict gateway merchant shopping_agent demo
Success: no issues found in 26 source files

$ mypy          # whole project via pyproject, strict = true
Success: no issues found in 55 source files
```

Strict covers source **and** tests. No suppression was added to reach this: the
two pre-existing `type: ignore`s were removed by typing the code properly.

> The gate as written says `mypy --strict gateway merchant shopping_agent verify demo`.
> `verify` is not a top-level package — the verifier is `gateway/verify.py`, already
> covered by the `gateway` argument. Renaming a module to match a path that has
> never existed would be the wrong repair. Recorded in `TRIAGE.md` §2.

## ☑ `make demo` twice — identical

```
$ make demo | grep -E "^[0-9]+ attempts"
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained

$ make demo | grep -E "^[0-9]+ attempts"
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained
```

Asserted at the CLI level by `test_running_the_cli_twice_produces_an_identical_report`,
which drives `main_async` twice and compares the whole of `report.json` — except
the chain tip, which is a hash over randomised ECDSA signatures and wall-clock
timestamps and *must* differ. The test asserts that it does.

## ☑ Recompute-from-audit-DB assertion

`test_the_report_recomputes_from_the_audit_chain_alone` rebuilds all six headline
numbers from persisted audit rows only, ignoring the in-memory results, and
requires them to agree. Two independent paths to the same figures; if they ever
disagreed, one of them would be lying.

`measure()` additionally reconciles the money **three ways** — signed receipts,
the spend ledger, the payment rail — and raises rather than printing a report if
they differ. That guard has fired for real: see `VERIFICATION_REPORT.md` §1.3.

## ☑ Fresh clone in `/tmp`, zero network

```
$ cp -r . /tmp/acg2 && cd /tmp/acg2
$ rm -rf .venv run demo/report.json demo/audit_chain.html .git __pycache__
$ cp .env.example .env
$ make setup
setup complete — Python 3.13.5
$ make demo
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained
  timeline → demo/audit_chain.html
  audit_chain.html: 112466 bytes
```

Every project-related environment variable was unset for that run, so nothing
leaked in from the parent shell.

**On "zero network":** `make setup` fetches pinned wheels from PyPI, as any Python
project must — claiming otherwise would be false. `make demo` opens no sockets,
and that is proved rather than asserted: `test_the_batch_opens_no_sockets`
replaces `socket.socket` with a class whose constructor raises, then runs the
entire batch and still gets the report line.

## ☑ `make mcp` — real MCP client calls

```
TOOLS: ['assemble_cart', 'check_product', 'check_serviceability',
        'complete_checkout', 'create_checkout', 'initiate_payment',
        'search_inventory']

search_inventory("running", {"max_price_inr": 1500, "size": "9"}) -> count: 4
  SF-ACC-001  Reflective Running Cap  ₹499.00  stock 40

initiate_payment -> status: captured | attempts: 1
{
  "vct": "receipt.payment.razorpay.1",
  "receipt_id": "rcpt_df1379400ae54548",
  "status": "captured",
  "idempotency_key": "c5ddade4b78a2251bedee980672fa9e8e4c0919e5e5b0779e45e39bacd44e789",
  "amount": 129900, "currency": "INR", "payee": "m_stridefit",
  "order_id": "order_fake_000001", "payment_id": "pay_fake_000001",
  "checkout_hash": "bb910f8bb1adf592d6617b4c8180ab9d4d7d58cd96bb0974d0fb44eb82943762",
  "attempts": 1, "failure_code": null
}

initiate_payment(garbage) -> {"error": "mandate.malformed",
                              "message": "expected a compact JWS with three segments"}
```

24 tests in `test_mcp_tools.py` cover every tool on both the happy and error path.

## ☑ `make serve` — real uvicorn

```
$ GATEWAY_PORT=8077 python -m gateway.app
INFO:     Uvicorn running on http://127.0.0.1:8077

$ curl -s /health
{"status":"ok","rail":"fake","narration":"templates","catalogue_skus":60,
 "audit_rows":0,"audit_chain_intact":true}

$ curl -s /trusted-surface/<hold>          # rendered to text
Approve a payment
Your shopping agent is asking for permission it does not already have.
₹4,999.00  to StrideFit Sportswear
Marathon Elite Carbon  SF-RUN-004  1  ₹4,999.00
₹4,999.00 is above the ₹1,500.00 per-checkout limit on this standing
authorisation, so it needs your approval.
Approve ₹4,999.00 once      Decline
Approving authorises exactly ₹4,999.00, only at StrideFit Sportswear, only for
this basket, and only for the next 10 minutes.
Your standing limit does not change.
This page contains no AI. The amount above is taken from a merchant-signed
Checkout Mandate and the explanation from the deterministic verifier.

$ curl -X POST /trusted-surface/<hold>/decision -d "decision=deny"
Declined. Nothing was charged.

$ curl -i -X POST /webhooks/razorpay -H 'X-Razorpay-Signature: forged' -d '{}'
HTTP/1.1 400 Bad Request
{"detail":"invalid webhook signature"}

$ curl -s /audit
{"verified": true, "rows_checked": 3, "broken_at": null, "tip_hash": "9ad50c52…"}

unknown hold      HTTP 404
bogus decision    HTTP 400
agent POST status HTTP 405
```

Those three audit rows are the rejected webhooks — a refusal is itself recorded.

## ☑ `make demo LIVE=1`

No Razorpay credentials are available to me, so this cannot run. It fails the way
it should — one actionable message, exit 2, no traceback:

```
$ make demo LIVE=1
  Cannot start: PAYMENT_RAIL=razorpay, but RAZORPAY_KEY_SECRET is not set.
  Put your Razorpay TEST-mode credentials in .env:
      RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxxxx
      RAZORPAY_KEY_SECRET=your_test_secret
  Walkthrough: docs/RAZORPAY_TESTING.md
$ echo $?
2
```

The **line-item review** of the live path against the Razorpay API and the SDK's
own resolved URLs is `VERIFICATION_REPORT.md` §5 — every endpoint, every field,
every error mapping, with the single unverifiable field flagged rather than
glossed. Summary:

| Call | Endpoint (resolved from the SDK) | Verdict |
|---|---|---|
| `create_order` | `POST /v1/orders` | ✅ every field documented |
| `fetch_order_payments` | `GET /v1/orders/{id}/payments` | ✅ source of truth after a timeout |
| `create_upi_payment_link` | `POST /v1/payment_links` | ⚠ `options.checkout.method.upi` unverified |
| webhook signature | HMAC-SHA256 over raw body, `compare_digest` | ✅ identical to `razorpay.Utility` |

`make keys` validates real credentials in ~30 seconds before committing to a full
`LIVE=1` run.

## ☑ All eight failure modes

```
$ pytest -k "failure_ or tamper_ or idempot or timeout_that"
72 passed, 470 deselected
```

| # | Mode | Test |
|---|---|---|
| 1 | Bank decline | `test_failure_1_bank_decline_falls_back_and_recovers` |
| 2 | API timeout | `test_failure_2_a_rail_timeout_opens_the_breaker_and_leaves_the_mandate_unspent` |
| 3 | Invalid mandate | `test_failure_3_a_malformed_mandate_is_typed_and_never_reaches_the_rail` (4 cases) |
| 4 | Budget breach | `test_failure_4_a_budget_breach_is_a_reason_object_not_an_exception` |
| 5 | Stock race | `test_failure_5_stock_selling_out_between_checkout_and_payment_declines_cleanly` |
| 6 | Duplicate submit | `test_failure_6_the_same_mandate_twice_returns_the_first_receipt` |
| 7 | Hallucinated SKU | `test_failure_7_a_nonexistent_sku_is_a_flat_not_found` |
| 8 | Out-of-band request | `test_failure_8_an_out_of_scope_purchase_is_escalated_and_can_be_denied` |
| 9 | **Timed-out-but-captured** | `test_a_timeout_that_actually_captured_never_double_charges` |

Each asserts the outcome **and** the audit row that records it.

## ☑ Chain tamper tests — six, not four

| Tamper | Test | Caught at |
|---|---|---|
| Edited payload | `test_tamper_a_edited_payload` | row 3 |
| Deleted row | `test_tamper_b_deleted_row` | row 5 (dangling link) |
| Reordered rows | `test_tamper_c_reordered_rows` | first row that moved |
| Re-hashed row + forged link | `test_tamper_d_rehashed_row_with_a_forged_prev_hash` | the next link |
| Spliced row | `test_tamper_e_forged_prev_hash_on_an_inserted_row` | the inserted row |
| Edited `human_reason` only | `test_verify_chain_catches_an_edited_human_reason` | row 2 |

Each drops the append-only DB triggers first, so they demonstrate catching
*determined* tampering rather than casual tampering.

## ☑ Idempotency — "exactly one charge"

```
test_failure_6_the_same_mandate_twice_returns_the_first_receipt
test_failure_6_five_submissions_still_charge_once
test_concurrent_presentations_charge_once                       (8 simultaneous)
test_a_timeout_that_actually_captured_never_double_charges
test_a_timeout_that_captured_is_settled_within_the_same_recovery_run
test_a_deferred_timeout_that_captured_is_caught_on_the_next_tick
test_recovery_never_double_charges_when_a_declined_attempt_secretly_captured
test_two_different_mandates_cannot_share_a_key
```

## ☑ No LLM on the money path

```
$ grep -rn "anthropic\|import llm\|from llm\|reason(" \
    gateway/verify.py gateway/payments.py gateway/recovery.py \
    gateway/razorpay_client.py merchant/checkout.py
$ echo $?
1
```

**Empty.** Enforced by three tests: the grep itself, an AST import-graph walk, and
a check that `config/policy.yaml`'s forbidden list has not been quietly edited.

## ☑ No secrets in history

```
$ pytest tests/test_security.py -k "credential or secret or env_file"
7 passed
```

The naive `git log -p | grep -iE "rzp_|key_secret|sk-ant"` is **not** empty and
cannot be for a project that names `RAZORPAY_KEY_ID` at all — it matches variable
names, `.env.example` placeholders, the SDK's `rzp_errors` alias, prose, and six
deliberately-fake fixtures. The meaningful scan looks for credential-*shaped*
values across full history against an explicit allowlist, plus a test that every
allowlist entry is obviously fake, plus a test that a planted key is caught.
`.env` was never committed (`--diff-filter=A` over all history).

## ☑ AP2 fidelity

`CONFORMANCE.md` — 24 PASS, 6 PARTIAL, 5 NOT IMPLEMENTED, 4 N/A, each with code
and test. `tests/test_ap2_fidelity.py` (26 tests) makes it a build failure:

| Spec | Code | Match |
|---|---|---|
| `mandate.checkout.open.1` · `mandate.checkout.1` | `vct.VCT_CHECKOUT_*` | ✅ |
| `mandate.payment.open.1` · `mandate.payment.1` | `vct.VCT_PAYMENT_*` | ✅ |
| `payment.budget` → `max`, `currency` | `BudgetConstraint` | ✅ |
| `payment.amount_range` → `min`, `max`, `currency` | `AmountRangeConstraint` | ✅ |
| `payment.allowed_payees` → `allowed` | `AllowedPayeesConstraint` | ✅ |
| `payment.execution_date` → `not_before`, `not_after` | `ExecutionDateConstraint` | ✅ |
| `payment.reference` → `conditional_transaction_id` | `ReferenceConstraint` | ✅ |
| `checkout.allowed_merchants` → `allowed` | `AllowedMerchantsConstraint` | ✅ |

A test asserts no constraint carries a field the spec does not define, and that
each docstring quotes the evaluation algorithm it implements.

## ☑ The three new gates

```
$ make redteam
21/21 blocked · Rs 0 unauthorised · 0 orders created

$ make bench
FALSE ACCEPTS: 0
p50 0.325 ms · p95 0.381 ms · p99 0.641 ms
3,385 verifications/sec single-threaded

$ make interop
✓ third-party AP2 agent purchase: COMPLETED
```

All three exit non-zero on failure, so they are gates rather than documents.

## ☑ Documentation reviewed against the code

`tests/test_docs.py` — 59 tests — makes drift a build failure: every internal link
resolves, the report line is byte-identical across four documents and matches
`report.json`, the documented budget / catalogue size / recovery cap / check count
/ tool count match the code, every backticked path and `make` target exists, every
environment variable the code reads is documented and every documented one has a
comment, and every test `SECURITY.md` cites exists.

## ☑ Commit history

```
Standout layers: red team, benchmark, third-party interop, HTML timeline
Triage: run every gate before changing anything
Add a Render blueprint for the public demo
Document $PORT in .env.example
Make the gateway deployable: honour $PORT
Add `make keys` — validate Razorpay credentials in 30 seconds
Add the clone command and repository URL to the docs
Phase 5+6: docs cross-checked against the code, and pinned
Phase 4: security review, webhook replay protection, SECURITY.md
Phase 3: AP2 fidelity, field by field
Phase 2: adversarial correctness, and two real findings
Phase 1: strict mypy, and .env was never actually read
BUILD_REPORT: acceptance checklist, run and passing
Fresh-clone smoke test, and prove the offline claim
Documentation: README, architecture, limitations, decisions
Gateway service, webhooks, and the full test suite
Shopping agent, human gate, and the six-attempt demo
Merchant role over MCP, and the composition root
Trusted Surface: the human gate, structurally non-agentic
Narration: the only door a language model gets
Merchant catalogue, carts, and the stock-race guard
Bounded recovery playbook and circuit breaker
Merchant Payment Processor: sha256 idempotency + attempt lease
Payment rail: one protocol, RazorpayRail and FakeRail
Deterministic verifier: every money decision, no LLM
Tamper-evident audit chain, policy loader, spend ledger
AP2 mandate model and ES256 JWS envelope
Scaffold: repo layout, pinned deps, policy file, MIT licence
```

---

## The one thing that is not proved

Nobody has watched a real order appear in a real Razorpay dashboard. The live path
is **correct-by-review** — endpoint by endpoint, field by field, against the
official API and the SDK's own source — and **not correct-by-observation**.
`VERIFICATION_REPORT.md` §5 says so explicitly rather than implying otherwise.

That is step 2 of "What you must do", and it is the only thing left.
