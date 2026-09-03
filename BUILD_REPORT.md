# Build report

**ap2-razorpay-gateway** — an AP2 v0.2 Merchant and Merchant Payment Processor for
Razorpay. Built 3 September 2026. Python 3.13.5 on macOS (targets ≥3.11).

**Status: complete.** Every item in the acceptance checklist passes. 295 tests, ruff
and mypy clean, and the demo prints the exact report line from a fresh clone with no
network, no API key and no Razorpay account.

```
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained
```

---

## 1. Stage status

| Stage | Status | Notes |
|---|---|---|
| Read the AP2 spec | ✅ | `specification.md`, `payment_mandate.md` fetched and read. Exact `vct` strings, five constraint types with their evaluation algorithms, the deterministic-verification MUST, and the non-agentic Trusted Surface requirement all transcribed into docstrings. |
| `pip install ap2` | ⚠️ **Vendored instead** | The PyPI `ap2` (0.1.1) is an unaffiliated third-party mirror by another author, exposing the A2A/ADK sample shapes rather than the v0.2 open/closed mandate model. Google's own SDK is not on PyPI. `ap2_min/` transcribes the needed subset directly from the spec. Decided in ~8 minutes; rationale in DECISIONS.md. |
| Razorpay API study | ✅ | Orders, Payment Links, Payments and Webhooks implemented against the official docs and SDK. One unverifiable field flagged in LIMITATIONS.md rather than guessed at. |
| A. Merchant role + MCP | ✅ | 60 SKUs / 3 merchants; exactly the seven required tools; stock-race guard. |
| B. Deterministic verifier | ✅ | 14 pure check functions, no LLM, no network, no I/O beyond a read-only ledger view. |
| C. Merchant Payment Processor | ✅ | `PaymentRail` protocol; `RazorpayRail` (test-mode-only, enforced in code) and `FakeRail` (programmable). |
| D. Trusted Surface, recovery, audit | ✅ | Non-agentic gate; 3-attempt bounded playbook + circuit breaker; append-only hash chain with DB triggers. |
| E. Shopping agent + reason writer | ✅ | `--scripted` and `--llm`; narration falls back to templates and never raises. |
| The demo | ✅ | Six attempts over a real in-process MCP connection. Every number measured. |
| Eight failure modes | ✅ | 34 tests; each asserts the outcome **and** the audit row. |
| Docs | ✅ | README, ARCHITECTURE, LIMITATIONS, DEMO, VIDEO_SCRIPT, DECISIONS, RAZORPAY_TESTING. |
| Fresh-clone smoke test | ✅ | `scripts/smoke.sh`, run and passing. |

## 2. What was built

```
 63 tracked files · 8,328 lines of source · 4,932 lines of tests · 15 commits
```

| Package | Lines | What it is |
|---|---|---|
| `ap2_min/` | 736 | Vendored AP2 v0.2 models: mandates, five typed constraints, receipts, roles, builders |
| `gateway/` | 3,551 | Verifier, payments, recovery, audit chain, Trusted Surface, Razorpay rail, webhooks, policy, composition root |
| `merchant/` | 1,032 | Catalogue, carts, stock re-check, the seven-tool MCP server |
| `llm/` | 337 | The single door a model gets |
| `shopping_agent/` | 704 | The agent, its MCP client, the human gate it cannot cross |
| `demo/` | 458 | The six-attempt batch and its measured report |
| `tests/` | 4,932 | 295 tests across 12 files |

## 3. Acceptance checklist

### ☑ `make setup && make test` — all tests pass

```
$ make test
........................................................................ [ 24%]
........................................................................ [ 48%]
........................................................................ [ 73%]
........................................................................ [ 97%]
.......                                                                  [100%]
295 passed in 1.98s
```

| File | Tests | Covers |
|---|---|---|
| `test_mandates.py` | 48 | JWS envelope, `alg:none`, HMAC/EC confusion, model invariants |
| `test_verify.py` | 40 | Every check, every boundary, determinism, purity |
| `test_failure_modes.py` | 34 | All eight modes + the no-LLM grep |
| `test_merchant.py` | 27 | Catalogue, carts, the stock/price re-check |
| `test_recovery.py` | 26 | Instrument ladder, breaker, backoff, exhaustion |
| `test_idempotency.py` | 23 | Duplicate, concurrent, retry-after-decline, retry-after-timeout |
| `test_audit_chain.py` | 23 | Four tampers, four catches |
| `test_trusted_surface.py` | 18 | The gate, and that approval is not an unlock |
| `test_payments.py` | 18 | The ALLOW gate, receipts, budget accounting |
| `test_demo.py` | 18 | The report line, and that it is measured |
| `test_receipts.py` | 10 | External verifiability, three-way reconciliation |
| `test_mcp_tools.py` | 10 | The seven tools, and the absent eighth |

### ☑ `make lint` — ruff and mypy clean

```
$ make lint
--- ruff ---
All checks passed!
47 files already formatted
--- mypy ---
Success: no issues found in 47 source files
lint clean
```

mypy runs with `disallow_untyped_defs`, `disallow_incomplete_defs`,
`check_untyped_defs`, `no_implicit_optional` and `strict_equality` over source **and**
tests. ruff runs `E,W,F,I,UP,B,SIM,C4,BLE,RUF` — `BLE` is enabled specifically so
every blind `except Exception` must carry a `noqa` and a justification. There are two,
both on the narration path.

### ☑ `make demo` — prints the exact report line

```
$ make demo | tail -1
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained
```

Full run in §4.

### ☑ The report line is not hardcoded

`demo/batch.py::measure()` in full — every number is a query over state the modules
produced:

```python
def measure(gateway: Gateway, results: list[AttemptResult]) -> Report:
    paid = sum(1 for r in results if r.status == STATUS_PAID)
    human_denied = sum(1 for r in results if r.status == STATUS_HUMAN_DENIED)
    recovered = sum(1 for r in results if r.recovered)

    # --- Three independent views of the money, which must agree. ------------
    receipts_say = sum(r.charged_amount for r in results)
    ledger_says = gateway.ledger.total_captured()
    audit_says = sum(
        int(row.payload["amount"])
        for row in gateway.audit.rows(event=Event.PAYMENT_RECEIPT_ISSUED)
        if row.payload.get("status") == "captured"
    )
    rail_says = (
        gateway.rail.captured_total() if isinstance(gateway.rail, FakeRail) else receipts_say
    )
    for name, value in (
        ("spend ledger", ledger_says),
        ("audit chain", audit_says),
        ("payment rail", rail_says),
    ):
        if value != receipts_say:
            raise AssertionError(
                f"reconciliation failed: signed receipts total ₹{paise_to_inr_str(receipts_say)} "
                f"but the {name} says ₹{paise_to_inr_str(value)}"
            )

    # --- Was any of it unauthorised? ---------------------------------------
    # Money is authorised when a captured receipt traces to an ALLOW from the
    # deterministic verifier. Anything captured without one is unauthorised
    # spend, by definition.
    allowed_mandates = {
        row.payload.get("checkout_id")
        for row in gateway.audit.rows(event=Event.DECISION)
        if row.payload.get("outcome") == "ALLOW"
    }
    authorised = sum(
        r.charged_amount for r in results if r.checkout_id in allowed_mandates and r.charged_amount
    )
    unauthorised = rail_says - authorised

    explained = sum(1 for r in results if r.human_reason.strip())
    return Report(
        attempts=len(results),
        paid=paid,
        human_denied=human_denied,
        recovered=recovered,
        unauthorised_spend=unauthorised,
        actions_explained=f"{explained}/{len(results)}",
    )
```

`r.status` comes from a **signed receipt**. `r.recovered` is
`RecoveryResult.recovered` — `captured and attempts > 1`. `human_denied` counts
decisions the simulated buyer actually made. There is no counter incremented as the
demo runs, no expected value, and no branch that knows which attempt was which.

The demo scripts exactly two world events, and neither decides an outcome:

```python
if index == 4:
    gateway.rail.decline(methods={METHOD_UPI}, times=1)   # the bank says no

def another_buyer_takes_the_last_one() -> None:           # inside attempt 6's
    gateway.catalog.set_stock(SOLD_OUT_SKU, 0)            # checkout→payment window
```

And four tests break the world and check the report changes with it:

| Test | Change | Report becomes |
|---|---|---|
| `test_breaking_the_rail_changes_the_report` | every payment declines | `paid == 0`, `recovered == 0` |
| `test_a_shopper_who_approves_changes_the_report` | the human says yes | `paid == 5`, `human_denied == 0` |
| `test_removing_the_stock_event_changes_attempt_six` | no concurrent buyer | attempt 6 paid; `paid == 5` |
| `test_the_money_reconciles_three_ways` | one phantom paise in the ledger | `measure()` raises `reconciliation failed` |

If the line were hardcoded, none of those could fail.

### ☑ All eight failure-mode tests pass, each asserting an audit row

```
$ .venv/bin/python -m pytest tests/test_failure_modes.py -o addopts="" -v

test_failure_1_bank_decline_falls_back_and_recovers                            PASS
test_failure_1_persistent_decline_stops_at_three_with_a_signed_receipt         PASS
test_failure_2_a_rail_timeout_opens_the_breaker_and_leaves_the_mandate_unspent PASS
test_failure_2_the_same_mandate_succeeds_on_the_next_tick                      PASS
test_failure_2_a_timeout_that_actually_captured_is_not_charged_twice           PASS
test_failure_3_a_malformed_mandate_is_typed_and_never_reaches_the_rail[]       PASS
test_failure_3_a_malformed_mandate_is_typed_and_never_reaches_the_rail[not-a-jwt]     PASS
test_failure_3_a_malformed_mandate_is_typed_and_never_reaches_the_rail[a.b.c]         PASS
test_failure_3_a_malformed_mandate_is_typed_and_never_reaches_the_rail[alg:none]      PASS
test_failure_3_a_forged_signature_is_rejected_at_the_boundary                  PASS
test_failure_3_a_mandate_missing_a_required_field_cannot_even_be_built         PASS
test_failure_3_a_mandate_from_an_unknown_key_is_rejected                       PASS
test_failure_4_a_budget_breach_is_a_reason_object_not_an_exception             PASS
test_failure_4_an_over_cap_closed_mandate_is_denied_not_gated                  PASS
test_failure_5_stock_selling_out_between_checkout_and_payment_declines_cleanly PASS
test_failure_5_a_price_change_between_checkout_and_payment_declines_cleanly    PASS
test_failure_5_stock_selling_out_mid_recovery_stops_the_retries                PASS
test_failure_6_the_same_mandate_twice_returns_the_first_receipt                PASS
test_failure_6_five_submissions_still_charge_once                              PASS
test_failure_6_a_duplicate_submit_is_audited_as_such                           PASS
test_failure_7_a_nonexistent_sku_is_a_flat_not_found                           PASS
test_failure_7_a_cart_containing_a_hallucinated_sku_is_refused                 PASS
test_failure_7_the_agent_replans_and_completes_the_purchase                    PASS
test_failure_8_an_out_of_scope_purchase_is_escalated_and_can_be_denied         PASS
test_failure_8_an_approved_escalation_completes_on_a_user_signed_mandate       PASS
test_failure_8_the_agent_cannot_approve_on_its_own_behalf                      PASS
test_no_language_model_on_the_money_path[gateway/verify.py]                    PASS
test_no_language_model_on_the_money_path[gateway/payments.py]                  PASS
test_no_language_model_on_the_money_path[gateway/recovery.py]                  PASS
test_the_money_path_modules_do_not_transitively_import_llm                     PASS
test_the_policy_file_agrees_with_the_code                                      PASS
test_narration_failure_does_not_stop_a_payment                                 PASS
test_every_audit_row_written_during_a_failure_is_explained                     PASS
test_the_chain_survives_every_failure_mode_in_one_run                          PASS

34 passed
```

Each asserts the outcome and the audit row. Examples:

| Mode | Outcome asserted | Audit row asserted |
|---|---|---|
| 1 | `recovered`, `methods_tried == [upi, payment_link]`, one capture | `PAYMENT_DECLINED`, `RECOVERY_METHOD_FALLBACK`, `RECOVERY_SUCCEEDED` |
| 2 | `deferred`, no receipt, idempotency record still `in_flight` | `RAIL_TIMEOUT`, `CIRCUIT_OPENED`, `CIRCUIT_DEFERRED` with `mandate_spent: false` |
| 3 | typed code, `rail.calls == []` | `MANDATE_REJECTED` carrying the code |
| 4 | `DENY`, reason object with `already_spent`/`over_by` | `DECISION` with `outcome: DENY` |
| 5 | `stock.unavailable`, `charged: false`, rail untouched | `STOCK_RECHECK_FAILED` |
| 6 | same `receipt_id`, one order, one capture | one `ORDER_CREATED`, two `PAYMENT_MANDATE_RECEIVED` |
| 7 | `product.not_found`, no cart, verifier never ran | `PRODUCT_NOT_FOUND`, `AGENT_REPLANNED` |
| 8 | `human_denied`, nothing charged | `CHECKOUT_UNRESOLVED`, `GATE_REQUESTED`, `GATE_DENIED`, `AGENT_ESCALATED` |

### ☑ `verify_chain()` catches a tampered row

`tests/test_audit_chain.py` **drops the append-only triggers first**, then breaks the
chain four ways — a tamper-evidence claim you have not tried to break is a hope:

| Test | Tamper | Result |
|---|---|---|
| `test_verify_chain_catches_an_edited_payload` | change one rupee in row 3 | `broken_at == 3`, "edited after it was written" |
| `test_verify_chain_catches_an_edited_human_reason` | rewrite the explanation, leave the numbers | `broken_at == 2` |
| `test_verify_chain_catches_a_deleted_row` | delete row 4 | `broken_at == 5`, "deleted, reordered or inserted" |
| `test_verify_chain_catches_a_spliced_forged_row` | forge row 2 *and* recompute its hash | `broken_at == 3` — the next link still fails |
| `test_verify_chain_catches_a_truncated_tail_only_via_the_tip` | delete the last row | chain verifies; the **tip hash** does not match |

That last one records the honest limit: truncation from the end is invisible to a
self-contained chain, which is why `tip_hash()` exists and `GET /audit` publishes it.

### ☑ A duplicate Payment Mandate yields one charge

```python
first  = merchant.initiate_payment(checkout_id, mandate)
second = merchant.initiate_payment(checkout_id, mandate)

assert second["replayed"] is True
assert second["payment_receipt"]["receipt_id"] == first["payment_receipt"]["receipt_id"]
assert fake_rail.captured_total() == inr(1299)   # exactly one charge
assert len(fake_rail.orders()) == 1              # exactly one order
```

Defended three ways, all tested: the stored terminal receipt, a capture probe over
every prior order under the key, and a DB-backed attempt lease for *simultaneous*
submits. Also covered: five submissions, eight concurrent threads, retry after a
decline, and retry after a timeout that had actually captured.

### ☑ No LLM on the money path

```
$ grep -rn "anthropic\|llm\." gateway/verify.py gateway/payments.py gateway/recovery.py
$ echo $?
1
```

No output. Enforced by `test_no_language_model_on_the_money_path`, which runs exactly
that grep, plus `test_the_money_path_modules_do_not_transitively_import_llm`, which
walks the AST import graph in case someone reaches the same place indirectly, plus
`test_the_policy_file_agrees_with_the_code`, which checks the forbidden list in
`config/policy.yaml` has not been quietly edited.

### ☑ Fresh-clone smoke test

`scripts/smoke.sh` copies the repo via `git archive HEAD` (tracked files only) into a
temp dir, refuses to proceed if a `.venv`, `.env`, database or report came along,
unsets every relevant environment variable, then runs the reviewer's path:

```
$ make smoke
→ source        /Users/princetomar/Razorpay/ap2-razorpay-gateway
→ temp clone    /var/folders/…/ap2-razorpay-gateway
→ copied via    git archive HEAD (tracked files only)
→ clean         no .venv, no .env, no database, no report

→ cp .env.example .env
→ make setup
setup complete — Python 3.13.5
→ make demo

[… full six-attempt run and audit trail …]

  reconciliation
      audit rows            132
      chain intact          yes
      chain tip             0e1a9f4929ba343aeefca95c01b39e7c…
      captured (ledger)     ₹4,096.00
      human decisions       1
      budget remaining      ₹904.00

6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained

✓ fresh clone produced the expected report line
✓ demo/report.json written
✓ fresh-clone smoke test passed
```

**On "zero network":** `make setup` fetches pinned wheels from PyPI, as any Python
project must. `make demo` itself opens no sockets, and that is proved rather than
claimed — `test_the_batch_opens_no_sockets` sabotages `socket.socket` so construction
raises, then runs the entire batch: six purchases, a human gate, a recovery, 132
audit rows, and not one socket.

### ☑ Documentation

| File | Contents |
|---|---|
| `README.md` | Problem, mermaid diagram, three-command run, AP2 role mapping, safety model, failure table, what's next |
| `ARCHITECTURE.md` | Roles, full request lifecycle, trust model, **"Where we deliberately do NOT use an LLM"** with the spec quote |
| `LIMITATIONS.md` | Plain JWS vs SD-JWT, no multi-hop delegation, synthetic catalogue, Razorpay test mode, UPI Reserve Pay is a closed pilot, and eleven more |
| `DEMO.md` | What the batch proves, the report line, why the numbers are real, video placeholder |
| `VIDEO_SCRIPT.md` | 5:00 beat sheet with the exact words |
| `DECISIONS.md` | ~50 autonomous choices with rationale |
| `docs/RAZORPAY_TESTING.md` | Test keys, `success@razorpay` / `failure@razorpay`, test cards, ngrok, OTP 754081, troubleshooting |

### ☑ Licence, gitignore, env template

- `LICENSE` — MIT, with AP2's Apache-2.0 attribution.
- `.gitignore` — `.env`, `*.db`, `__pycache__`, `.venv`, `*.pem`, `run/`, tool caches.
  `git ls-files` confirms no `.env`, no `*.pem`, nothing matching `secret`.
- `.env.example` — every variable with a comment. Copied verbatim it runs the whole
  demo; nothing needs filling in unless you want the live check.

### ☑ Small titled commits

```
0721ccb Fresh-clone smoke test, and prove the offline claim
cadc747 Documentation: README, architecture, limitations, decisions
501d27e Gateway service, webhooks, and the full test suite
c99f593 Shopping agent, human gate, and the six-attempt demo
5296d19 Merchant role over MCP, and the composition root
1d7a548 Trusted Surface: the human gate, structurally non-agentic
b8311af Narration: the only door a language model gets
17899f7 Merchant catalogue, carts, and the stock-race guard
3885ccc Bounded recovery playbook and circuit breaker
9ec3245 Merchant Payment Processor: sha256 idempotency + attempt lease
9dede87 Payment rail: one protocol, RazorpayRail and FakeRail
68c1407 Deterministic verifier: every money decision, no LLM
31a1593 Tamper-evident audit chain, policy loader, spend ledger
5402f7e AP2 mandate model and ES256 JWS envelope
3ccf702 Scaffold: repo layout, pinned deps, policy file, MIT licence
```

## 4. `make demo` output

```
AP2 × Razorpay — agentic checkout batch
  rail            fake
  narration       fake
  agent mode      scripted
  daily budget    ₹5,000.00
  per purchase    ₹1,500.00
  merchants       m_stridefit, m_lumen, m_pixelbyte
  catalogue       60 SKUs

  ✓ attempt 1  running shoes, size 9, under ₹1,500, ship to 560001
      SF-RUN-001   StrideFit Sportswear          ₹1,299.00
      status: paid
      re-planned 1× before finding a real product
      Paid ₹1,299.00 by upi.

  ✓ attempt 2  running shorts under ₹1,000
      SF-APP-001   StrideFit Sportswear            ₹899.00
      status: paid
      Paid ₹899.00 by upi.

  ⨯ attempt 3  the carbon-plate marathon racing shoe
      SF-RUN-004   StrideFit Sportswear          ₹4,999.00
      status: human_denied
      escalated to a human: checkout.amount_exceeds_standing_limit
      The buyer declined ₹4,999.00 at StrideFit Sportswear. Nothing was charged.
    · the bank will decline the next UPI attempt

  ✓ attempt 4  a cast iron dosa tawa
      LM-KIT-002   Lumen Home & Kitchen          ₹1,199.00
      status: paid
      recovered after 2 attempts, one idempotency root
      Paid ₹1,199.00 by payment_link after 1 failed attempt(s).

  ✓ attempt 5  a magnetic car phone mount
      PB-MOB-003   PixelByte Electronics           ₹699.00
      status: paid
      Paid ₹699.00 by upi.
    · another buyer just took the last SF-ACC-001

  ⨯ attempt 6  a reflective running cap
      SF-ACC-001   StrideFit Sportswear            ₹499.00
      status: declined_stock
      Reflective Running Cap (SF-ACC-001) is down to 0 in stock; the cart needs 1
```

The audit trail follows — 132 rows, every one with a plain-English reason. Attempt 4
in full (the recovery), which is the section worth reading:

```
  shopping_agent         agent.plan
           ↳ The agent is looking for: a cast iron dosa tawa
  merchant               merchant.cart_assembled
           ↳ Assembled a cart of 1 item(s) from Lumen Home & Kitchen for ₹1,199.00.
  merchant               merchant.checkout_created
           ↳ Signed a checkout for ₹1,199.00 at Lumen Home & Kitchen, guaranteed at
             that price for 15 minutes.
  merchant               merchant.checkout_mandate_received
           ↳ The agent presented the buyer's standing checkout authorisation for a
             ₹1,199.00 cart.
  merchant               merchant.checkout_receipt_issued
           ↳ Checkout confirmed for ₹1,199.00 at Lumen Home & Kitchen.
  merchant               merchant.payment_mandate_received
           ↳ Received a payment mandate for ₹1,199.00 to m_lumen, signed by the
             buyer's agent.
  verifier               14 checks passed
           ↳ signature, vct, presenter_role, not_expired, vct, not_expired,
             key_binding, currency, allowed_payees, amount_range, budget,
             execution_date, checkout_reference, nonce
  verifier               verifier.decision
           ↳ Approved ₹1,199.00 to Lumen Home & Kitchen: signed by the delegated
             agent key, within the per-purchase limit and the remaining daily
             budget, bound to this checkout, first use of this token.
  merchant_payment_processor recovery.started
           ↳ Paying ₹1,199.00 with at most 3 attempt(s), trying upi then
             payment_link then card.
  merchant_payment_processor mpp.order_created
           ↳ Created a ₹1,199.00 order on the fake rail for m_lumen, to be paid by upi.
  merchant_payment_processor mpp.payment_attempt
           ↳ Attempt 1 on order_fake_000003 using upi.
  merchant_payment_processor mpp.payment_declined
           ↳ upi was declined on order_fake_000003: the bank declined this payment.
             No money moved.
  merchant_payment_processor recovery.method_fallback
           ↳ upi failed (the bank declined this payment); falling back to payment_link.
  merchant_payment_processor mpp.order_created
           ↳ Created a ₹1,199.00 order on the fake rail for m_lumen, to be paid by
             payment_link.
  merchant_payment_processor mpp.payment_attempt
           ↳ Attempt 2 on order_fake_000004 using payment_link.
  merchant_payment_processor mpp.payment_captured
           ↳ Captured ₹1,199.00 for Lumen Home & Kitchen (pay_fake_000004) by
             payment_link.
  merchant_payment_processor mpp.payment_receipt_issued
           ↳ Issued a signed receipt: ₹1,199.00 captured after 2 attempt(s).
  merchant_payment_processor recovery.succeeded
           ↳ Recovered: payment_link succeeded on attempt 2 after upi failed. Same
             idempotency root throughout, so nothing was charged twice.
  merchant               merchant.stock_decremented
           ↳ Reduced stock after payment cleared: LM-KIT-002=21
```

Two orders. One capture. One receipt.

```
  reconciliation
      audit rows            132
      chain intact          yes
      chain tip             0e1a9f4929ba343aeefca95c01b39e7c…
      captured (ledger)     ₹4,096.00
      human decisions       1
      budget remaining      ₹904.00

6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained
```

## 5. Things that went beyond the brief

Four of these exist because a test failed, which is the only reason worth having.

**RFC 7800 key binding.** The buyer's open mandate names the agent's public key in
`cnf`; the verifier compares it against whoever signed the closed mandate. Without
it, a standing authorisation that appears in any log line is bearer authority — every
other constraint describes *what* may be bought, not *who* may buy it. A mandate with
no `cnf` is rejected rather than waved through.

**A DB-backed attempt lease.** `test_concurrent_presentations_charge_once` failed:
eight simultaneous presentations of one mandate all read "no receipt yet" and all
charged. A stored receipt cannot help at t=0; only serialisation can. Now a
conditional `UPDATE` inside `BEGIN IMMEDIATE`, correct across processes, with an
expiry so a crashed holder cannot wedge a mandate.

**`GateView`.** `test_failure_8_the_agent_cannot_approve_on_its_own_behalf` failed —
passing `SimulatedShopper` to the agent put `.surface`, and therefore `decide()`, one
dot away from code whose entire safety story is that it cannot approve its own
payments. The agent now receives a one-method object. LIMITATIONS.md is explicit that
Python cannot make this a hard capability boundary and names the three that are.

**Nonce ownership rather than presence.** `nonce_owner()` returns *which* mandate
burned a nonce. The same mandate re-presenting is a retry; a different one is a
replay. A boolean cannot tell them apart, and the difference is what makes "the
circuit breaker deferred your payment, try again next tick" actually work.

**A currency check.** `1500 USD < 1500 INR` is true. Without an explicit comparison
every numeric bound underneath it is meaningless.

**Role-aware key verification.** "Was this signed by the user?" is a different
question from "is this signature valid?". A merchant key signing a buyer
authorisation verifies perfectly and is still refused.

**`human_reason` inside the chain hash.** The brief's formula covered
`(actor, event, payload, ts)`. Rewriting the explanation of a payment without touching
its numbers is exactly the tamper a dispute turns on.

**Append-only database triggers**, so the chain tests demonstrate catching
*determined* tampering rather than casual tampering.

**A socket-sabotage test**, so "zero network" is a property rather than a sentence.

## 6. Known gaps

All in LIMITATIONS.md. The four worth naming here:

1. **Plain JWS, not SD-JWT.** A privacy reduction, not an integrity one. Confined to
   one module by design.
2. **The audit chain detects tampering; it cannot prevent it**, and truncation from
   the end is invisible without an external anchor. `tip_hash()` is published so one
   can be added.
3. **No Credential Provider.** The buyer's instrument is effectively Razorpay's test
   rail rather than being held by a third party.
4. **`RazorpayRail.complete_test_payment` polls; it does not pay.** There is no
   Razorpay API that completes a payment on a customer's behalf. Inventing one would
   have made the live demo smoother and the code wrong.

## 7. Untested by me

Everything in this repository was run and verified offline. One thing was not, because
it needs credentials I do not have:

**`make demo LIVE=1` against a real Razorpay sandbox.** The code path is implemented
from the official documentation and SDK, the test-mode key guard is enforced in code,
and `FakeRail` mirrors the sandbox's `success@razorpay` / `failure@razorpay`
behaviour — but nobody has yet watched a real order appear in a real dashboard. That
is the one check left, and it is step 2 below.
