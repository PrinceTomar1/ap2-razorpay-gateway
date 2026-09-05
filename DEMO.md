# The demo

```bash
make demo
```

Zero network. No API key. No Razorpay account. Runs in about a second.

```
6 attempts · 4 paid · 1 human-denied · 1 recovered · ₹0 unauthorised · 6/6 explained
```

---

## The scenario

A buyer gives their shopping agent a standing authorisation: **₹5,000 a day, ₹1,500
per purchase, three merchants, shipping to 560001, valid 24 hours.** They sign it
once. Then six things happen.

| # | What the agent tries | What happens | What it proves |
|---|---|---|---|
| 1 | Running shoes under ₹1,500 — but the buyer's note named a SKU that does not exist | Re-plans past the fake SKU, buys `SF-RUN-001` for ₹1,299 | **Failure mode 7.** A hallucinated product costs one lookup. No cart, nothing signed, the verifier never runs. |
| 2 | Running shorts under ₹1,000 | Buys `SF-APP-001` for ₹899 | The clean path, and the budget arithmetic accumulating. |
| 3 | The ₹4,999 carbon-plate racing shoe | The agent recognises it is over its cap, presents its standing mandate, gets `unresolved_constraint`, and hands a human the decision. **The human declines.** Nothing charged. | **Failure mode 8.** The gate is a gate. An agent that asks gets a human; an agent that forces would get a `DENY`. |
| 4 | A cast iron dosa tawa, ₹1,199 | The bank declines UPI. Recovery falls back to a payment link and succeeds — **on the same idempotency root**. | **Failure mode 1.** Bounded recovery: 3 attempts max, one root, one charge. |
| 5 | A magnetic car phone mount, ₹699 | Buys it | A third merchant, and the budget still holding. |
| 6 | A reflective running cap, ₹499 | Another buyer takes the last one *between the signed checkout and the payment*. Clean decline, nothing charged. | **Failure mode 5.** The stock race, in the window it actually occurs in. |

All three merchants are used. Total charged: ₹4,096 of a ₹5,000 budget, leaving ₹904.

## Why the numbers are real

This is the part worth checking, because a demo whose output is the same whether or
not the code works is a screenshot.

**The demo scripts exactly two world events, and measures everything else.**

```python
# demo/batch.py — the whole of the scripting
if index == 4:
    gateway.rail.decline(methods={METHOD_UPI}, times=1)   # the bank says no

def another_buyer_takes_the_last_one() -> None:          # inside attempt 6's
    gateway.catalog.set_stock(SOLD_OUT_SKU, 0)           # checkout→payment window
```

Both inject an *event*. Neither decides an outcome. Whether the purchase then
recovers, and whether the sold-out cart declines cleanly, is settled by
`gateway/recovery.py` and `merchant/checkout.py`.

**Every number is a query over state the modules produced:**

```python
def measure(gateway: Gateway, results: list[AttemptResult]) -> Report:
    paid          = sum(1 for r in results if r.status == STATUS_PAID)
    human_denied  = sum(1 for r in results if r.status == STATUS_HUMAN_DENIED)
    recovered     = sum(1 for r in results if r.recovered)

    # Three independent views of the money, which must agree.
    receipts_say = sum(r.charged_amount for r in results)   # signed receipts
    ledger_says  = gateway.ledger.total_captured()          # the spend ledger
    audit_says   = sum(... PAYMENT_RECEIPT_ISSUED rows ...) # the audit chain
    rail_says    = gateway.rail.captured_total()            # the payment rail
    for name, value in (...):
        if value != receipts_say:
            raise AssertionError(f"reconciliation failed: ...")

    # Money is authorised only when a capture traces to an ALLOW from the verifier.
    allowed_mandates = {row.payload["checkout_id"]
                        for row in gateway.audit.rows(event=Event.DECISION)
                        if row.payload["outcome"] == "ALLOW"}
    authorised   = sum(r.charged_amount for r in results
                       if r.checkout_id in allowed_mandates and r.charged_amount)
    unauthorised = rail_says - authorised

    explained = sum(1 for r in results if r.human_reason.strip())
```

`r.status` is set from a **signed receipt**, not from an intention. `r.recovered` is
`RecoveryResult.recovered`, which is `captured and attempts > 1`. `human_denied`
counts decisions the simulated buyer actually made. There is no counter incremented
as the demo goes along, no expected value, and no branch that knows which attempt
was which.

**And there are tests that break the world and check the report changes with it:**

| Test | Change | Report becomes |
|---|---|---|
| `test_breaking_the_rail_changes_the_report` | every payment declines | `paid == 0`, `recovered == 0` |
| `test_a_shopper_who_approves_changes_the_report` | the human says yes | `paid == 5`, `human_denied == 0` |
| `test_removing_the_stock_event_changes_attempt_six` | no concurrent buyer | attempt 6 is paid; `paid == 5` |
| `test_the_money_reconciles_three_ways` | inject one phantom paise into the ledger | `measure()` raises `reconciliation failed` |

If the report line were hardcoded, none of those tests could fail.

## What the audit trail looks like

Attempt 4 — the recovery — as printed by `make demo`:

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

Two orders. One capture. One receipt. `make demo --verbose` prints each of the
fourteen checks on its own line instead of folding the passing ones.

The run ends with a reconciliation block:

```
  reconciliation
      audit rows            132
      chain intact          yes
      chain tip             7d800396fe5cbdea09fcb501c59634ed…
      captured (ledger)     ₹4,096.00
      human decisions       1
      budget remaining      ₹904.00
```

`chain tip` is the hash a third party would record to detect later truncation — see
[LIMITATIONS.md](LIMITATIONS.md#truncation-from-the-end-is-invisible).

## The report file

`demo/report.json` is written every run:

```json
{
  "attempts": 6,
  "paid": 4,
  "human_denied": 1,
  "recovered": 1,
  "unauthorised_spend": 0,
  "actions_explained": "6/6",
  "line": "6 attempts · 4 paid · 1 human-denied · 1 recovered · ₹0 unauthorised · 6/6 explained",
  "rail": "fake",
  "audit_rows": 132,
  "audit_chain_intact": true,
  "audit_chain_tip": "7d800396fe5cbdea09fcb501c59634ed…",
  "captured_paise": 409600,
  "attempts_detail": [ … one entry per attempt, with its receipt id … ]
}
```

## Variations

```bash
make demo --verbose      # every verifier check on its own line
make demo LIVE=1         # attempts 1 and 4 against the real Razorpay TEST sandbox
.venv/bin/python -m demo.batch --llm     # a model picks the products
.venv/bin/python -m demo.batch --json    # report.json only
```

`--llm` needs `ANTHROPIC_API_KEY` and `LLM_PROVIDER=anthropic` in `.env`. The model
chooses *which SKU to look at*, from the list the merchant returned. It cannot choose
an amount, a payee, or a second charge — those are decided in `gateway/verify.py`
from signed data. With no key set, everything falls back to deterministic templates
and the run is identical minus the prose.

`LIVE=1` runs attempts 1 and 4 only. Razorpay's sandbox cannot be told to time out
on demand, so the rest of the batch is about behaviour only the simulator can
produce. See [docs/RAZORPAY_TESTING.md](docs/RAZORPAY_TESTING.md).

## Video

📹 **Pitch video:** <!-- PASTE THE HOSTED LINK HERE BEFORE SUBMITTING -->

Also in the repo at
[`video/ap2-razorpay-pitch.mp4`](video/ap2-razorpay-pitch.mp4) — 4:57, 1920×1080,
narrated by Prince Tomar over ten slides, mastered to −11.2 LUFS. Build notes and
the audio chain are in [video/README.md](video/README.md).

**Repository:** https://github.com/PrinceTomar1/ap2-razorpay-gateway

**Live gateway:** https://ap2-razorpay-gateway.onrender.com — the Trusted Surface
approval page and `/audit` running for real. Free tier, so the first request
takes ~45 seconds to wake it.

**Slides:** `slides/index.html` — open it, press `F` for fullscreen, arrow keys to
advance. Nine slides matching the beats in VIDEO_SCRIPT.md.
