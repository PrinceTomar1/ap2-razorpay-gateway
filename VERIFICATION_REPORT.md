# Verification report

An adversarial pass over the whole project, reviewing it as somebody trying to
reject it rather than somebody hoping it passes. Every check below was run, its
real output read, and whatever was wrong was fixed and re-run.

**Nine real bugs were found.** Four of them would have been visible to a reviewer;
one would have broken the single live check this submission asks its author to
perform. All are fixed, each with a regression test.

| | Before | After |
|---|---|---|
| Tests | 295 | **516** |
| mypy | partial (`disallow_untyped_defs`) | **full `strict`**, no suppressions added |
| Test files | 12 | 18 |
| `.env` | documented but **never read** | read, tested, and its absence is not an error |
| Demo determinism | broke under a real `.env` | pinned, and proven across runs |

---

## 1. What was found

### 1.1 `.env` was never read — the live check could not have worked

Every document says to put Razorpay keys in `.env`. `make setup` creates one. No
code ever loaded it. A reviewer following the instructions would have hit:

```
RuntimeError: PAYMENT_RAIL=razorpay needs RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
in .env. See docs/RAZORPAY_TESTING.md.
```

— a traceback telling them to do the thing they had just done.

**Fixed.** `gateway/config.py`: a dependency-free loader that never overrides a
real environment variable and never evaluates anything. 24 tests, including that
`$(...)`, backticks and `${...}` all stay literal text.

### 1.2 mypy was not strict, and was being actively blinded

A `follow_imports = "skip"` override covered pyjwt, fastmcp, anthropic and mcp —
**all of which ship `py.typed`**. It silently turned their return values into
`Any`. Removing it and enabling `strict = true` surfaced nine errors, including
genuinely unreachable code in `merchant/service.py` carrying a `# pragma: no
cover` that had been hiding the fact it was dead.

**Fixed** at source. No `type: ignore` was added; two pre-existing ones were
removed by typing the code properly.

### 1.3 Honouring `.env` broke demo determinism — and the guard caught it

With `.env` read, `GATEWAY_DB=run/gateway.db` made the demo persistent. A second
run inherited the first run's spend:

```
AssertionError: reconciliation failed: signed receipts total ₹4,096.00
but the spend ledger says ₹121,802.00
```

The three-way reconciliation refused to print a report rather than printing a
wrong one — the guard working exactly as intended.

**Fixed.** The demo pins its own in-memory database whatever the environment says.
`make serve` still persists.

### 1.4 The test suite was not hermetic

Tests picked up the developer's `.env`, so three of them started sharing a file on
disk. The first fix — deleting the variables — did not work, because
`load_dotenv` then filled them back in. Pinning safe values is the only version
that actually isolates.

### 1.5 A refused mandate killed the whole checkout — a DoS

One malformed or over-limit presentation marked the checkout `declined`
permanently. Anyone able to reach `initiate_payment` with a bad token could kill a
stranger's cart, and a legitimate agent that fixed its mistake could not retry.

**Fixed.** `status` describes the checkout, not the outcome of one presentation.

### 1.6 A stock re-check failure did the same

The merchant's signed price guarantee still stood for its full window, and the
re-check runs again on every attempt — so killing the checkout lost a sale for no
safety benefit.

### 1.7 Recovery retried failures that could never succeed

A rejected *request* — bad amount, order already paid, suspended account — cannot
be fixed by a different instrument. The playbook walked the whole ladder anyway,
failing identically twice more and creating two orders for nothing.

**Fixed.** `RailDeclined` carries `retryable`; Razorpay 400s are non-retryable.

### 1.8 Webhook replay was not defended

A valid signature proves a delivery came from Razorpay, not that it has not
arrived before. Razorpay retries on any non-2xx, so duplicates are *normal*, and
anyone who captures one body can replay it for as long as the secret lives.

**Fixed.** Deduplicated on `X-Razorpay-Event-Id`, **after** signature verification
so an unauthenticated caller cannot claim an id to suppress the real webhook.

### 1.9 Two AP2 fidelity drifts

- The open Checkout Mandate carried ad-hoc fields instead of the spec's typed
  `constraints` array with `checkout.allowed_merchants`.
- `payment.allowed_payees` held bare strings; the spec's `allowed` array holds
  merchant objects.

Both fixed, and fidelity is now a test rather than a claim.

### 1.10 Two of my own tests were wrong, and one was vacuous

Recorded because a review that only reports other people's mistakes is not a
review.

- A JWS-leak detector matched any string with two dots and 120 characters — it
  fired on the verifier's own English explanation. A test that flags prose proves
  nothing. Now requires the `eyJ` header prefix and no whitespace, and there is a
  test *of the detector*.
- The secret-scanner's bait was written as a literal, which put a
  credential-shaped string into git history that the scanner then correctly
  flagged. Assembled at runtime now; the offending commit was amended out.

---

## 2. Phase 1 — it runs, everywhere

### ☑ `make lint` — ruff and mypy (strict) clean

```
$ make lint
--- ruff ---
All checks passed!
53 files already formatted
--- mypy ---
Success: no issues found in 53 source files
lint clean
```

`pyproject.toml` now reads:

```toml
[tool.mypy]
strict = true
warn_unreachable = true
ignore_missing_imports = false

[[tool.mypy.overrides]]
module = ["razorpay", "razorpay.*"]   # the ONLY dependency without py.typed
ignore_missing_imports = true
```

Strict covers `ap2_min`, `gateway`, `merchant`, `llm`, `shopping_agent`, `demo`
**and** `tests`. ruff runs `E,W,F,I,UP,B,SIM,C4,BLE,RUF`; `BLE` is on so every
blind `except Exception` needs a justified `noqa` — there are two, both on the
narration path.

### ☑ `make test` — 516 pass, 0 skipped, 0 xfail

```
$ make test
........................................................................ [ 14%]
........................................................................ [ 28%]
........................................................................ [ 42%]
........................................................................ [ 56%]
........................................................................ [ 70%]
........................................................................ [ 84%]
........................................................................ [ 98%]
........                                                                 [100%]
516 passed in 3.71s
```

| File | Tests | Covers |
|---|---|---|
| `test_docs.py` | 59 | Docs must agree with the code — counts, paths, targets, links |
| `test_mandates.py` | 53 | JWS envelope, `alg:none`, HMAC/EC confusion, model invariants |
| `test_correctness.py` | 51 | The adversarial "what input breaks this?" pass |
| `test_verify.py` | 40 | Every check, every boundary, determinism, purity |
| `test_security.py` | 39 | Signature bypass, replay, secrets, the Trusted Surface as attack surface |
| `test_failure_modes.py` | 34 | All eight modes + the no-LLM grep |
| `test_merchant.py` | 27 | Catalogue, carts, the stock/price re-check |
| `test_recovery.py` | 26 | Instrument ladder, breaker, backoff, exhaustion |
| `test_ap2_fidelity.py` | 26 | Every spec value, as a table the build enforces |
| `test_mcp_tools.py` | 24 | Seven tools, happy **and** error path each |
| `test_config.py` | 24 | `.env` parsing, live-key guard, configuration errors |
| `test_idempotency.py` | 23 | Duplicate, concurrent, retry-after-decline, retry-after-timeout |
| `test_audit_chain.py` | 23 | Tamper detection |
| `test_demo.py` | 21 | The report line, and that it is measured |
| `test_trusted_surface.py` | 18 | The gate, and that approval is not an unlock |
| `test_payments.py` | 18 | The ALLOW gate, receipts, budget accounting |
| `test_receipts.py` | 10 | External verifiability, three-way reconciliation |

No test is skipped, xfailed, or filtered. `addopts` contains `--strict-markers`
and no `-k`.

### ☑ `make demo` twice — identical report line

```
$ make demo | grep -E "^[0-9]+ attempts"
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained

$ make demo | grep -E "^[0-9]+ attempts"
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained
```

Asserted at the CLI level by
`test_running_the_cli_twice_produces_an_identical_report`, which drives
`main_async` twice and compares the whole of `report.json` — everything except the
chain tip, which is a hash over randomised ECDSA signatures and wall-clock
timestamps and *must* differ, and the test asserts that it does.

`test_the_demo_ignores_gateway_db_and_starts_from_an_empty_ledger` proves the
regression from §1.3 cannot return.

### ☑ Fresh-clone smoke test

```
$ make smoke
→ copied via    git archive HEAD (tracked files only)
→ clean         no .venv, no .env, no database, no report

→ cp .env.example .env
→ make setup
setup complete — Python 3.13.5
→ make demo

  ✓ attempt 1  running shoes, size 9, under ₹1,500, ship to 560001
      SF-RUN-001   StrideFit Sportswear          ₹1,299.00
      status: paid
      re-planned 1× before finding a real product
  ✓ attempt 2  running shorts under ₹1,000            ₹899.00   paid
  ⨯ attempt 3  the carbon-plate marathon racing shoe  ₹4,999.00 human_denied
      escalated to a human: checkout.amount_exceeds_standing_limit
    · the bank will decline the next UPI attempt
  ✓ attempt 4  a cast iron dosa tawa                  ₹1,199.00 paid
      recovered after 2 attempts, one idempotency root
  ✓ attempt 5  a magnetic car phone mount             ₹699.00   paid
    · another buyer just took the last SF-ACC-001
  ⨯ attempt 6  a reflective running cap               ₹499.00   declined_stock

  reconciliation
      audit rows            132
      chain intact          yes
      chain tip             28fb10eff3eaca1d7bf1cabd7a5ecd4a…
      captured (ledger)     ₹4,096.00
      human decisions       1
      budget remaining      ₹904.00

6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained

✓ fresh clone produced the expected report line
✓ demo/report.json written
✓ fresh-clone smoke test passed
```

**On "zero network".** `make setup` fetches pinned wheels from PyPI, as any Python
project must — claiming otherwise would be false. `make demo` itself opens no
sockets, and that is *proved*, not asserted:
`test_the_batch_opens_no_sockets` replaces `socket.socket` with a class whose
constructor raises, then runs the entire batch — six purchases, a human gate, a
recovery, 132 audit rows — and the report line still comes out.

### ☑ `make mcp` — real tool list and real calls

Driven through a real `fastmcp.Client`, exactly as an external AP2 agent would:

```
TOOLS: ['assemble_cart', 'check_product', 'check_serviceability',
        'complete_checkout', 'create_checkout', 'initiate_payment',
        'search_inventory']

search_inventory("running", {"max_price_inr": 1500, "size": "9"}) -> count: 4
{
  "sku": "SF-ACC-001",
  "name": "Reflective Running Cap",
  "merchant_id": "m_stridefit",
  "price_inr": "499.00",
  "price_paise": 49900,
  "stock": 40,
  "serviceable_pincodes": ["560001", "560034", "560103", "400001", "110001"],
  "return_days": 14
}

complete_checkout -> status: confirmed

initiate_payment -> status: captured | attempts: 1
{
  "vct": "receipt.payment.razorpay.1",
  "receipt_id": "rcpt_df1379400ae54548",
  "status": "captured",
  "payment_mandate_id": "pm_bf51c61d164f4542",
  "idempotency_key": "c5ddade4b78a2251bedee980672fa9e8e4c0919e5e5b0779e45e39bacd44e789",
  "amount": 129900,
  "currency": "INR",
  "payee": "m_stridefit",
  "order_id": "order_fake_000001",
  "payment_id": "pay_fake_000001",
  "method": "upi",
  "checkout_hash": "bb910f8bb1adf592d6617b4c8180ab9d4d7d58cd96bb0974d0fb44eb82943762",
  "attempts": 1,
  "failure_code": null,
  "ts": "2026-09-03T08:04:55Z"
}
receipt_jws (first 60): eyJhbGciOiJFUzI1NiIsImtpZCI6ImtleV9tcHBfcmF6b3JwYXkiLCJ0eXAi ...

initiate_payment(garbage) -> {"error": "mandate.malformed",
                              "message": "expected a compact JWS with three segments"}
```

Every tool now has an error-path test as well as a happy path — 24 tests in
`test_mcp_tools.py`.

### ☑ `make serve` — real uvicorn, real HTTP

```
$ GATEWAY_PORT=8077 .venv/bin/python -m gateway.app
INFO:     Uvicorn running on http://127.0.0.1:8077

$ curl -s /health
{"status":"ok","rail":"fake","narration":"templates","catalogue_skus":60,
 "audit_rows":0,"audit_chain_intact":true}

$ curl -s /webhooks/razorpay/health
{"configured":false,"note":"Set RAZORPAY_WEBHOOK_SECRET to accept webhooks.
 Without it the gateway polls order.payments instead..."}

$ curl -i -X POST /webhooks/razorpay -H 'X-Razorpay-Signature: forged' -d '{...}'
HTTP/1.1 400 Bad Request
{"detail":"invalid webhook signature"}

$ curl -s "/audit?limit=5"
{"verified": true, "rows_checked": 3, "broken_at": null,
 "tip_hash": "9ad50c52aa1b081c6587baac31e00ede85536570f815db32954919bf825edc72"}
```

Those three audit rows are the rejected webhooks — a refusal is itself recorded.

The Trusted Surface, over real HTTP, with a pending ₹4,999 hold:

```
$ curl -s /                       # the pending list
<li><a href="/trusted-surface/gate_0983e952271f4aa6">₹4,999.00 at StrideFit Sportswear</a></li>

$ curl -s /trusted-surface/gate_0983e952271f4aa6      # rendered to text
Approve a payment
Your shopping agent is asking for permission it does not already have.
₹4,999.00
to StrideFit Sportswear
Marathon Elite Carbon  SF-RUN-004  1  ₹4,999.00
₹4,999.00 is above the ₹1,500.00 per-checkout limit on this standing
authorisation, so it needs your approval.
checkout.amount_exceeds_standing_limit
Approve ₹4,999.00 once      Decline
Approving authorises exactly ₹4,999.00, only at StrideFit Sportswear, only for
this basket, and only for the next 10 minutes.
Your standing limit does not change.
AP2 Trusted Surface · checkout chk_eafd672877c14169
This page contains no AI. The amount above is taken from a merchant-signed
Checkout Mandate and the explanation from the deterministic verifier.

$ curl -s /trusted-surface/<hold>/status
{"status": "pending", "amount": 499900, "amount_inr": "4,999.00",
 "constraint": "checkout.amount_exceeds_standing_limit",
 "checkout_mandate_jws": null, "payment_mandate_jws": null}

$ curl -X POST /trusted-surface/<hold>/decision -d "decision=deny"
Declined. Nothing was charged.

$ curl -s /trusted-surface/<hold>/status
{'status': 'denied', 'payment_mandate_jws': None, 'checkout_mandate_jws': None}

unknown hold      HTTP 404
bogus decision    HTTP 400
agent POST status HTTP 405
```

### ☑ `make demo LIVE=1`

No Razorpay credentials are available to me, so this cannot run. It now fails the
way it should — one actionable message, no traceback, exit 2:

```
$ make demo LIVE=1
  Cannot start: PAYMENT_RAIL=razorpay, but RAZORPAY_KEY_SECRET is not set.
  Put your Razorpay TEST-mode credentials in .env:
      RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxxxx
      RAZORPAY_KEY_SECRET=your_test_secret
  Get them from the Razorpay dashboard in Test Mode:
      Settings -> API Keys -> Generate Test Key
  Walkthrough: docs/RAZORPAY_TESTING.md
$ echo $?
2
```

The line-item review of the live path is §5.

---

## 3. Phase 2 — correctness

### The no-LLM grep, exactly as specified

```
$ grep -rn "anthropic\|from llm\|import llm\|reason(" \
    gateway/verify.py gateway/payments.py gateway/recovery.py \
    gateway/razorpay_client.py merchant/checkout.py
$ echo $?
1
```

**Empty.** Backed by three tests: the grep itself, an AST import-graph walk, and a
check that `config/policy.yaml`'s forbidden list has not been quietly edited.

### Named correctness tests

**Verifier**

| Property | Test |
|---|---|
| A re-signed token with a raised ceiling is refused | `test_a_re_signed_token_with_a_raised_ceiling_is_rejected` |
| A body edited after signing is refused | `test_a_body_edited_after_signing_is_rejected` |
| Expiry is UTC and exact to the second | `test_expiry_is_evaluated_in_utc_and_one_second_past_is_rejected` |
| Expiry is identical in UTC / Kolkata / Los Angeles | `test_expiry_is_not_affected_by_the_local_timezone` |
| An open mandate where a closed one is required escalates, never pays | `test_an_open_mandate_where_a_closed_one_is_required_is_an_escalation_not_a_pass` |
| `amount_range` inclusive at min, at max, refused one paise over | `test_amount_range_bounds_are_inclusive_exactly_as_the_spec_says` (6 cases) |
| Below the floor is refused too | `test_below_the_minimum_is_also_refused` |
| Budget = spent + amount, ledger moves only on **capture** | `test_budget_is_spent_plus_amount_and_the_ledger_moves_only_on_capture` |
| A mismatched checkout hash is denied even for an identical cart | `test_a_mismatched_checkout_hash_is_denied_even_for_an_identical_cart` |
| The reference check is sha-256 of *this* signed checkout | `test_the_reference_check_compares_against_this_checkouts_hash` |
| A second mandate reusing a nonce is a replay | `test_a_second_mandate_reusing_a_burned_nonce_is_a_replay` |
| The same mandate twice is settled by idempotency, not replay | `test_the_same_mandate_twice_is_settled_by_idempotency_not_by_replay` |
| alg confusion / `alg:none` at the **service** boundary | `test_alg_confusion_is_refused_at_the_service_boundary`, `test_alg_none_is_refused_at_the_service_boundary`, `test_only_es256_is_accepted` (5 algorithms) |

**Idempotency — both layers**

| Property | Test |
|---|---|
| Duplicate `initiate_payment` | `test_failure_6_the_same_mandate_twice_returns_the_first_receipt` |
| **Timeout that actually captured** | `test_a_timeout_that_actually_captured_never_double_charges` |
| …settled inside the same recovery run | `test_a_timeout_that_captured_is_settled_within_the_same_recovery_run` |
| …and across a deferral, on the next tick | `test_a_deferred_timeout_that_captured_is_caught_on_the_next_tick` |
| …when a *fallback* attempt follows a secret capture | `test_recovery_never_double_charges_when_a_declined_attempt_secretly_captured` |
| Eight simultaneous presentations | `test_concurrent_presentations_charge_once` |
| Key collision across mandates | `test_two_different_mandates_cannot_share_a_key` |

`FakeRail.timeout_after_capture` records the capture and *then* raises
`RailTimeout`, so the caller genuinely does not know. A real sandbox cannot be
asked to do that on demand — which is the entire reason FakeRail exists.

**Audit chain — four distinct tampers, plus two**

| Tamper | Test | Caught at |
|---|---|---|
| Edited payload | `test_tamper_a_edited_payload` | row 3 |
| Deleted row | `test_tamper_b_deleted_row` | row 5 (dangling link) |
| **Reordered rows** | `test_tamper_c_reordered_rows` | first row that moved |
| **Re-hashed row with a forged link** | `test_tamper_d_rehashed_row_with_a_forged_prev_hash` | the next link |
| Spliced row with forged `prev_hash` | `test_tamper_e_forged_prev_hash_on_an_inserted_row` | the inserted row |
| Edited `human_reason` only | `test_verify_chain_catches_an_edited_human_reason` | row 2 |

Each drops the append-only triggers first, so they demonstrate catching
*determined* tampering rather than casual tampering.

**Recovery**

| Property | Test |
|---|---|
| Never changes amount, currency, payee or key | `test_recovery_never_changes_the_amount_or_the_payee` |
| Stays inside the mandate's `amount_range` | `test_recovery_stays_inside_the_mandates_amount_range` |
| Stops at exactly 3 | `test_recovery_stops_at_exactly_three_attempts` |
| An audit row per attempt, numbered in order | `test_recovery_writes_an_audit_row_for_every_attempt` |
| A signed `payment_failed` receipt | `test_failure_1_persistent_decline_stops_at_three_with_a_signed_receipt` |
| **Does not retry a non-retryable failure** | `test_recovery_does_not_retry_a_non_retryable_failure` |
| …and ordinary recovery still works | `test_a_retryable_decline_still_walks_the_ladder` |

**Stock race — ordering, observed not assumed**

`test_the_stock_recheck_runs_after_confirmation_and_before_the_rail` traces the
real call order. `test_a_sold_out_cart_never_creates_an_order` asserts
`rail.calls == []`. `test_a_sold_out_cart_does_not_burn_the_mandate` restocks and
shows the same mandate still works.

**Prompt injection**

`test_a_malicious_product_name_cannot_move_money`,
`test_an_agent_that_obeys_an_injection_is_still_stopped_by_the_verifier`,
`test_llm_output_cannot_influence_a_decision_or_a_database_write`,
`test_a_malicious_product_name_is_escaped_on_the_trusted_surface`.

### The demo is measured, not printed

`demo/batch.py::measure()` derives every number from module return values, and
reconciles the money three ways before it will print anything:

```python
receipts_say = sum(r.charged_amount for r in results)   # signed receipts
ledger_says  = gateway.ledger.total_captured()          # the spend ledger
audit_says   = sum(... PAYMENT_RECEIPT_ISSUED rows ...) # the audit chain
rail_says    = gateway.rail.captured_total()            # the payment rail
for name, value in (...):
    if value != receipts_say:
        raise AssertionError(f"reconciliation failed: ...")

allowed_mandates = {row.payload["checkout_id"]
                    for row in gateway.audit.rows(event=Event.DECISION)
                    if row.payload["outcome"] == "ALLOW"}
authorised   = sum(r.charged_amount for r in results if r.checkout_id in allowed_mandates)
unauthorised = rail_says - authorised
```

**Added this pass:** `test_the_report_recomputes_from_the_audit_chain_alone`
rebuilds all six headline numbers from persisted audit rows only, ignoring the
in-memory results, and requires them to agree. If the objects and the durable
record ever disagreed, one of them would be lying.

Four tests break the world and check the report changes:

| Test | Change | Report becomes |
|---|---|---|
| `test_breaking_the_rail_changes_the_report` | every payment declines | `paid == 0` |
| `test_a_shopper_who_approves_changes_the_report` | the human says yes | `paid == 5` |
| `test_removing_the_stock_event_changes_attempt_six` | no concurrent buyer | `paid == 5` |
| `test_the_money_reconciles_three_ways` | one phantom paise | `measure()` raises |

---

## 4. Phase 3 — AP2 fidelity, field by field

Sources: `docs/ap2/specification.md`, `payment_mandate.md`, `checkout_mandate.md`,
`flows.md` from github.com/google-agentic-commerce/AP2 (Apache-2.0). Enforced by
`tests/test_ap2_fidelity.py` (26 tests).

### `vct` claims — all match

| Spec value | Code | Match |
|---|---|---|
| `mandate.checkout.open.1` | `vct.VCT_CHECKOUT_OPEN` | ✅ |
| `mandate.checkout.1` | `vct.VCT_CHECKOUT_CLOSED` | ✅ |
| `mandate.payment.open.1` | `vct.VCT_PAYMENT_OPEN` | ✅ |
| `mandate.payment.1` | `vct.VCT_PAYMENT_CLOSED` | ✅ |

The `vct` types are closed `Literal`s, so an unknown version is refused while the
payload is being parsed — before the `vct` check runs.

### Payment Mandate constraints — all match

| Spec `type` | Spec fields | Code fields | Match |
|---|---|---|---|
| `payment.budget` | `max`, `currency` | `max`, `currency` | ✅ |
| `payment.amount_range` | `min`, `max`, `currency` | `min`, `max`, `currency` | ✅ |
| `payment.allowed_payees` | `allowed` (merchant objects) | `allowed: list[Payee]` — **fixed this pass** | ✅ |
| `payment.execution_date` | `not_before`, `not_after` | `not_before`, `not_after` | ✅ |
| `payment.reference` | `conditional_transaction_id` | `conditional_transaction_id` | ✅ |

A test asserts no constraint carries a field the spec does not define, and that
every docstring quotes the evaluation algorithm it implements.

### Checkout Mandate constraints

| Spec `type` | Status |
|---|---|
| `checkout.allowed_merchants` | ✅ implemented exactly — `allowed` array of merchant objects. **Fixed this pass**; it was ad-hoc fields. |
| `checkout.line_items` | ❌ not implemented — the merchant's signed cart already pins every SKU and quantity. LIMITATIONS.md. |
| `x-checkout.amount_ceiling` | **Extension.** AP2 defines no per-checkout ceiling. |
| `x-checkout.ship_to` | **Extension.** AP2 defines no delivery address. |

The spec permits new constraints: *"To define a new constraint, the following MUST
be specified: A uniquely defined `type`. A Schema... The evaluation algorithm."*
Both extensions carry all three, and the `x-` prefix guarantees no future AP2
constraint can collide. Asserted by
`test_each_extension_documents_a_schema_and_an_evaluation_algorithm`.

### Documented divergences — each asserted, so they stay decisions

| Divergence | Why | Where |
|---|---|---|
| Plain JWS, not SD-JWT | Privacy reduction, not integrity. Confined to one module. | LIMITATIONS.md; comment at the top of `gateway/mandates.py` |
| `delegate_chain`, not `delegate_payload` | The spec's `{"...": digest}` shape only has meaning inside an SD-JWT | LIMITATIONS.md |
| Integer paise, not W3C float | Float money is a correctness bug | LIMITATIONS.md |
| `allowed_payees` matches on `id` | A look-alike name is the attack an allow-list exists to stop | LIMITATIONS.md |
| Single-hop delegation | One agent, one buyer | LIMITATIONS.md |
| Three constraints unimplemented | Named individually | LIMITATIONS.md |

### `unresolved_constraint` and the two flows

`flows.md`: *"A Human Not Present flow can be turned into a Human Present flow by
the Merchant... returning an `unresolved_constraint` error and bringing the User
back into the loop to approve the closed Mandates."*

The spec does not fix the error's field names. Ours uses the spec's literal
`"error": "unresolved_constraint"` plus what an agent actually needs to act:

```json
{
  "error": "unresolved_constraint",
  "constraint": "checkout.amount_exceeds_standing_limit",
  "human_reason": "₹4,999.00 is above the ₹1,500.00 per-checkout limit ...",
  "checkout_id": "chk_...", "amount": 499900, "currency": "INR",
  "hold_id": "gate_...", "approval_url": "http://.../trusted-surface/gate_..."
}
```

`test_both_ap2_flows_are_reachable` proves Human-Not-Present is the default (the
buyer signs open mandates once) and Human-Present is what the gate produces (the
buyer signs **closed** mandates, verified as `mandate.checkout.1` and
`mandate.payment.1`).

### The non-agentic Trusted Surface

`test_the_trusted_surface_imports_no_language_model` walks the parsed syntax tree
— not a substring search, because the module's own docstring says "no import path
to `llm/`" and a naive grep flags that sentence. No import, no name, no attribute
reaching `llm`, `anthropic`, `openai` or `ReasonWriter`.

ARCHITECTURE.md quotes both spec requirements with the file cited, asserted by
`test_the_architecture_document_quotes_the_spec_on_both_requirements`.

---

## 5. Line-item review of the live Razorpay path

`make demo LIVE=1` cannot run without credentials, so the live path was reviewed
against the official API reference and the installed SDK, endpoint by endpoint and
field by field.

### `create_order` → `POST /v1/orders`

Resolved from the SDK: `razorpay.resources.order.Order.create` → `post_url(self.base_url)`
where `base_url = URL.V1 + URL.ORDER_URL` = `/v1/orders`. ✅

| Field sent | Type | Correct? |
|---|---|---|
| `amount` | integer **paise** | ✅ Razorpay amounts are in the smallest currency unit. Our internal unit is already paise, so no conversion — and therefore no rounding bug. |
| `currency` | `"INR"` | ✅ ISO 4217 |
| `receipt` | `key[:40]` | ✅ Razorpay caps `receipt` at 40 chars. Carries our idempotency key so a dashboard order traces to the mandate. |
| `notes` | `{reference, checkout_hash, open_mandate, protocol}` | ✅ Free-form key/value; each value truncated to 40 chars |
| `payment_capture` | `1` | ✅ Auto-capture on authorisation — correct here, because the verifier has already decided |

Response fields read: `id`, `amount`, `currency`, `receipt`, `status`, `notes` —
all documented on the Order entity. ✅

### `fetch_order_payments` → `GET /v1/orders/{id}/payments`

`Order.payments` → `"{}/{}/payments".format(self.base_url, order_id)`. ✅

Returns `{"entity": "collection", "count": n, "items": [...]}`; we read
`items`. Each item is a Payment entity, from which we read `id`, `order_id`,
`amount`, `currency`, `status`, `method`, `error_code`, `error_description` — all
documented. `status` values we branch on are `captured` and `failed`, the two
terminal states. ✅

**This is the source of truth after a timeout**, and it is the same data Razorpay
uses to build the `payment.captured` / `payment.failed` webhook — which is why
polling is a correct fallback rather than a compromise.

### `create_upi_payment_link` → `POST /v1/payment_links`

`PaymentLink.create` → `post_url(self.base_url)` where `base_url = /v1/payment_links`. ✅

| Field sent | Correct? |
|---|---|
| `amount`, `currency` | ✅ paise + ISO 4217 |
| `accept_partial: false` | ✅ documented; partial payment would break the amount binding |
| `description` (≤2048) | ✅ documented limit |
| `reference_id` (≤40) | ✅ documented; our order id |
| `notify: {sms: false, email: false}` | ✅ documented — we have no customer contact and must not invent one |
| `reminder_enable: false` | ✅ documented |
| `options.checkout.method.upi: "1"` | ⚠️ **The one field not verified against a live sandbox.** Documented by Razorpay for restricting methods on the hosted page. If it behaves differently the link still works — it simply offers more methods than intended. Flagged in LIMITATIONS.md rather than presented as certain. |

Response fields read: `id`, `short_url`, `status`, `amount`, `reference_id` — all
documented. ✅

### `complete_test_payment` — polling, not pretending

**There is no Razorpay API that completes a payment on a customer's behalf.** A
payment is made by a human on the hosted page. So the implementation creates a
link, prints it, and polls `order.payments` until a terminal payment appears or
the deadline passes — raising `RailTimeout`, not `RailDeclined`, on expiry,
because "nobody paid yet" and "the bank said no" are different facts.

Test VPAs used are Razorpay's documented ones: `success@razorpay`,
`failure@razorpay`. Test-mode OTP `754081` is documented in
`docs/RAZORPAY_TESTING.md`.

### Error mapping

| SDK exception | Mapped to | Reasoning |
|---|---|---|
| `BadRequestError` (4xx) | `RailDeclined(retryable=False)` | A statement about the *request* — bad amount, order already paid. Another instrument cannot fix it. |
| `GatewayError`, `ServerError` (5xx) | `RailUnavailable` | Nothing happened; safe to retry |
| `requests.Timeout` | `RailTimeout` | Outcome unknown — the dangerous one |
| `requests.RequestException` | `RailUnavailable` | Could not reach Razorpay |
| `SignatureVerificationError` | re-raised | Never silently swallowed |

### Webhook signature — `gateway/webhooks.py`

Razorpay's scheme is `HMAC-SHA256(raw_request_body, webhook_secret)`, hex, compared
against the `X-Razorpay-Signature` header. Our implementation:

```python
expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
return hmac.compare_digest(expected, signature)
```

✅ Identical to `razorpay.Utility.verify_webhook_signature` → `verify_signature`,
which is `hmac.new(key, msg, hashlib.sha256).hexdigest()` + `hmac.compare_digest`.
Implemented directly so the check does not depend on the SDK being importable and
so the constant-time compare is visible.

- Verified over the **raw bytes**, before any parsing —
  `test_the_signature_covers_the_exact_bytes_not_the_parsed_json`.
- An empty secret validates nothing — `test_an_empty_secret_never_validates_anything`.
- Bad signature → **400** so Razorpay retries, plus an audit row.
- Handled events: `payment.captured`, `payment.failed`, `order.paid`,
  `payment_link.paid`. Payload shape `{"event": ..., "payload": {"payment":
  {"entity": {...}}}}` — read defensively, with six malformed shapes tested.

### What is genuinely unverified

Nobody has watched a real order appear in a real dashboard. The code is
correct-by-review against the documented API and the SDK's own source; it has not
been correct-by-observation. That is step 2 of "What you must do".

---

## 6. Phase 4 — security

Full threat model in [SECURITY.md](SECURITY.md): ten threats, each mitigation, and
the test that proves it. `test_security_document_links_each_threat_to_a_real_test`
verifies every test name cited there actually exists.

### The secrets grep, run literally

```
$ git log -p | grep -iE "key_id|key_secret|sk-|rzp_"
```

**Not empty** — and it cannot be for a project that names `RAZORPAY_KEY_ID` at
all. Every one of the ~60 matches is:

- a variable or parameter name (`key_id`, `key_secret`, `RAZORPAY_KEY_ID`)
- a `.env.example` placeholder (`rzp_test_xxxxxxxxxxxxxx`)
- the Razorpay SDK's module alias (`rzp_errors`)
- documentation prose ("refuses any key id that is not `rzp_test_`")
- one of six deliberately-fake test fixtures

The meaningful check scans full history for credential-*shaped* values:

```
$ .venv/bin/python -m pytest tests/test_security.py -k "credential or secret or env_file"
7 passed
```

```python
CREDENTIAL_PATTERNS = (r"rzp_live_[A-Za-z0-9]{6,}", r"rzp_test_[A-Za-z0-9]{6,}",
                       r"sk-ant-[A-Za-z0-9_\-]{10,}", r"sk-[A-Za-z0-9]{20,}")
real = found_in_git_history - KNOWN_FAKE_CREDENTIALS
assert real == set()
```

Backed by two more tests: every allowlist entry must *look* obviously fake (so the
scanner cannot be silenced by adding a real key), and a planted key must be
caught. `.env` is gitignored and `--diff-filter=A` over all history confirms it was
never committed.

### Every Phase 4 check

| Check | Result |
|---|---|
| Signature bypass paths | All JWS handling in one module, enforced by test; the only unverified read is the `kid` used to select the key |
| `alg` confusion / `none` | Refused for 5 algorithms at the service boundary |
| Mandate replay | Nonce ownership; `test_a_second_mandate_reusing_a_burned_nonce_is_a_replay` |
| **Webhook replay** | **Fixed this pass** — deduplicated after verification |
| Idempotency-key collision | Full sha-256, 64-bit random mandate ids, 500-id collision test |
| XSS on the Trusted Surface | `html.escape` on every value; script and img payloads tested |
| Prompt injection | Proven inert three ways, including a fully-compromised agent |
| LLM influence on decisions or writes | None — only a narration column |
| Secrets in history | Clean, by a scanner that is itself tested |

---

## 7. Phase 5 — documentation

Every document was read against the implementation. Drift found and fixed:

| Drift | Fix |
|---|---|
| README claimed "300+ tests", "13 test files" | Now correct, and pinned by a test |
| README repo map missing `config.py`, `SECURITY.md` | Added |
| `VERIFICATION_REPORT.md` linked but absent | This file |
| **VIDEO_SCRIPT did not contain the exact report line** it tells you to read | Added as a fenced block |
| ARCHITECTURE described ad-hoc checkout fields | Now the typed constraints array |
| ARCHITECTURE did not mention `retryable`, `.env`, or webhook dedup | Added |
| SECURITY cited a test name that did not exist | Corrected |
| DECISIONS contradicted the code on `allowed_payees` | Corrected, with a pointer to the later change |
| BUILD_REPORT counts stale | Marked superseded, pointing here |
| LIMITATIONS silent on in-memory webhook dedup | Added |

**Drift is now a build failure.** `tests/test_docs.py` (55 tests) checks: every
internal link resolves; the report line is byte-identical in README, DEMO,
VIDEO_SCRIPT and BUILD_REPORT and matches `report.json`; the documented budget,
catalogue size, recovery cap, check count (14) and tool count (7) match the code;
every backticked source path exists; every `make` target named exists; every
environment variable the code reads is documented in `.env.example` **and** every
documented one has an explanatory comment; every test SECURITY.md cites exists.

Three of those tests failed on first run and found real errors — the missing
report line in VIDEO_SCRIPT, the missing `merchant/checkout.py` reference in
ARCHITECTURE, and the wrong test name in SECURITY.

---

## 8. Phase 6 — scoring, as the panel would

Scored honestly. I have not given myself a 10 anywhere, and the justification names
the weakness rather than hiding it.

### Problem Taste — 9/10

AP2 is the actual emerging standard (Google, September 2025, 60+ organisations
including Mastercard, Visa, PayPal, Coinbase). There is no AP2 implementation for
Razorpay or UPI, and Razorpay is building agentic payments with NPCI and OpenAI —
so the gap is real and current, not invented for a submission. The problem
statement is one sentence a payments engineer would agree with immediately: *an AI
agent holding a payment credential is unbounded liability.* The Track 01 fit is
exact rather than retrofitted — "explainable, bounded and gated" describes the
architecture, not a feature added to satisfy it.

**Weakness:** the catalogue is synthetic and no real merchant was consulted about
whether this is the shape of the problem they have. That is the difference between
a well-chosen problem and a validated one.

### Build Quality — 9/10

516 tests, full `strict` mypy over source and tests, ruff clean, zero
suppressions added during hardening. Verified end to end rather than assumed: a
real MCP client, a real uvicorn process with real curls, a fresh clone from `git
archive`, and an offline claim proved by sabotaging `socket.socket`. Nine real
bugs found and fixed during this pass, each with a regression test. Documentation
drift is now a build failure.

Raised from what I would have scored it at the start of this pass (7): mypy was
not strict and was being actively blinded, `.env` was never read, the demo was
non-deterministic under a real `.env`, and three tests were sharing state.

**Weakness:** the live Razorpay path is correct-by-review, not
correct-by-observation. Single-process guarantees, no auth on the HTTP surface, in
memory keys. All named in LIMITATIONS.md and SECURITY.md rather than glossed.

### AI Judgment — 9/10

The strongest axis, and the one the track explicitly rewards. The load-bearing
decision is *not* using a model: the verifier is deterministic because the spec
says it MUST be, and because a verifier is a classifier over a small fully
specified domain where code is simply better. That is not a comment — it is
`grep`, an AST import-graph walk, a policy file, and three tests.

Where a model does run, both uses are bounded by construction: narration computes
the template first and falls back on any failure (`reason()` never raises), and
product selection validates the model's answer against the SKUs the merchant
returned. Prompt injection is proven inert three ways, including the case where
the agent is assumed fully compromised.

**Weakness:** catalogue search is a keyword match where a model would genuinely
help and would be entirely safe. That is a deliberate scope decision, recorded in
LIMITATIONS.md — but it means the project demonstrates restraint more thoroughly
than it demonstrates good judgement about when a model *is* the right tool.

### Failure Recovery — 9/10

Nine failure modes, each asserting both the outcome and the audit row that records
it. The hard one is handled properly: a timeout that actually captured, natively
simulated by `FakeRail.timeout_after_capture`, defended by a capture probe over
every order ever created under the idempotency key. Three rail error types, not
one, because "declined" and "we don't know" demand different responses. A
deferral deliberately issues no receipt so the mandate stays spendable. Non-retryable
failures stop immediately instead of failing identically twice more.

**Weakness:** recovery is bounded and correct but not *observable* — there is no
metric, alert or dashboard, so an operator would learn about a rail outage by
reading audit rows.

### Summary

| Axis | Score |
|---|---|
| Problem Taste | 9 |
| Build Quality | 9 |
| AI Judgment | 9 |
| Failure Recovery | 9 |

No axis is below 8, so no axis needed remediation work beyond what is above.

---

## 9. Commit history

```
git log --oneline
```

```
Phase 5: documentation cross-checked against the code, and pinned
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

## 10. What you must do

Three things. Nothing else is outstanding.

**1. Put your Razorpay test keys in `.env`.**

```bash
cd ap2-razorpay-gateway
cp .env.example .env        # if you have not already
```

```dotenv
PAYMENT_RAIL=razorpay
RAZORPAY_KEY_ID=rzp_test_XXXXXXXXXXXXXX     # Dashboard → Test Mode → Settings → API Keys
RAZORPAY_KEY_SECRET=your_test_secret
```

Confirm the dashboard toggle says **Test Mode**. A key starting `rzp_live_` is
refused in code. `.env` is now actually read — that was bug §1.1.

**2. Run `make demo LIVE=1` once and confirm attempts 1 and 4 succeed.**

You get a payment link per attempt. Pay **attempt 1** with UPI id
`success@razorpay`. For **attempt 4**, pay the first link with
`failure@razorpay` — recovery will log the decline, fall back, and hand you a
second link; pay that with `success@razorpay`. Expect exactly one capture and a
`recovery.succeeded` audit row. Full walkthrough and troubleshooting in
[docs/RAZORPAY_TESTING.md](docs/RAZORPAY_TESTING.md).

This is the only thing in the project I could not verify myself.

**3. Record the video, push, and submit.**

- Record the 5-minute walkthrough from [VIDEO_SCRIPT.md](VIDEO_SCRIPT.md) — it has
  the words, the timings, and what to show at each beat. Add the link to the
  placeholder at the bottom of [DEMO.md](DEMO.md).
- `git remote add origin … && git push -u origin main`. The tree is clean, `.env`
  is gitignored, and a full-history scan for credential-shaped strings is part of
  the test suite.
- Submit the buildathon form before **5 September 2026**.

One suggestion for the recording: the single most persuasive thing on screen is
attempt 4's audit slice — two `mpp.order_created` rows and one
`mpp.payment_captured` row. That is the whole idempotency argument in three lines
of log, and it is far easier to show than to explain.
