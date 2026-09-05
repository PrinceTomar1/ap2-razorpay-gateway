# Video script — 5:00

Read aloud over a screen recording. Stop at 4:45. Times are cumulative.

**Before you start:** open three things — this repo in an editor, a terminal in the
repo root, and a browser tab you will not need until 1:30. Run `make demo` once to
warm it. Delete `demo/report.json` so the run you record writes it fresh.

---

## 0:00 — The problem, and a number

> An AI agent that holds a payment credential is unbounded liability.
>
> Not a hypothetical one. Give a shopping agent a card number and there is no
> technical limit on what it can spend, where, how many times, or what it does when
> a payment fails halfway through. The controls we have — "are you sure?", a cap in a
> dashboard, a support queue — are controls on a *human* checkout flow. An agent hits
> them at machine speed with nobody watching.
>
> Google published the Agent Payments Protocol in September 2025 with sixty-plus
> organisations behind it — Mastercard, Visa, PayPal, Coinbase — to fix exactly this.
> A purchase carries a *mandate*: a signed, constrained, verifiable statement of what
> the buyer actually authorised. Every party checks it before money moves.
>
> There is no AP2 implementation for Razorpay, and none for UPI. Razorpay is building
> agentic payments with NPCI and OpenAI. This is that missing piece.

*(Screen: README.md, top.)*

---

## 0:40 — The architecture, and the one decision that matters

*(Screen: the mermaid diagram in README.md. Point as you go.)*

> Five roles in AP2. I implement two of them properly — Merchant, and Merchant
> Payment Processor — plus the Trusted Surface, and I simulate a Shopping Agent so
> there is something to verify against.
>
> The buyer signs two mandates once: five thousand rupees a day, fifteen hundred per
> purchase, three merchants, twenty-four hours. The agent holds a keypair those
> mandates name. Every purchase is a closed mandate signed under them.

*(Point at the green box.)*

> This is the verifier. Everything green here is code a language model may not run
> in. That is not my preference — it is the specification.

*(Screen: `gateway/verify.py`, the module docstring.)*

> "When this document refers to validation or processing for a particular role, it
> **MUST** happen in deterministic code, regardless of whether the role is agentic
> or not."
>
> A verifier is a classifier over a small, fully specified domain. Does this signature
> check out. Is this integer below that integer. Is this string in that list. Code
> does that perfectly and explains itself. A model does it probabilistically, can't
> be audited afterwards, and adds a network dependency to the one path that must
> never be down.
>
> And it's not a claim — it's a test.

*(Terminal, paste this. It prints nothing.)*

```bash
grep -rn "anthropic\|llm\." gateway/verify.py gateway/payments.py gateway/recovery.py
```

> Nothing. And `tests/test_failure_modes.py` runs that grep and fails the build if it
> ever stops being nothing.
>
> Fourteen checks run on a clean purchase. Signature. Exact `vct` claim. Key binding,
> so a leaked standing mandate isn't bearer authority. Payee on the allow-list.
> Amount in range. Running spend within budget. Bound to *this* checkout by hash.
> Nonce not seen before.

---

## 1:30 — Live: `make demo`

*(Terminal. Type it and let it run.)*

```bash
make demo
```

> Six attempts. Zero network — no API key, no Razorpay account, in-memory rail.

*(Scroll to attempt 1.)*

> One. The buyer's note named a model number that doesn't exist. The agent asks,
> gets `product.not_found`, and re-plans. No cart, nothing signed, verifier never
> ran. That's failure mode seven and it cost one dictionary lookup.

*(Attempt 3.)*

> Three. Four thousand nine hundred and ninety-nine rupees. That's over the buyer's
> fifteen-hundred cap, and the agent *knows* it — so instead of forcing a mandate it
> presents its standing authorisation and gets AP2's `unresolved_constraint` back.

*(Browser: open the approval URL from the audit line, or show `make serve` running.)*

> This is the Trusted Surface. Non-agentic — there's no model on this page. The
> amount comes out of the merchant's signed Checkout Mandate. The explanation comes
> out of the deterministic verifier.
>
> And read the small print: approving authorises exactly this amount, only at this
> merchant, only for this basket, for ten minutes. It does not raise the limit.
> `amount_range` with min equals max. A budget equal to that same amount, so it funds
> one payment and never a second.
>
> Here, the buyer declines. Nothing is charged.

*(Back to terminal, attempt 4.)*

> Four. The bank declines UPI. Recovery falls back to a payment link — you can see
> both orders in the trail, `order_fake_000003` and `_000004` — and it succeeds on
> attempt two.
>
> Two orders. **One capture.** Same idempotency root, which is `sha256` of the payment
> mandate id. And before it ever creates that second order it asks the rail whether
> the first one captured — because the genuinely dangerous case in payments isn't a
> decline, it's a *timeout*, where you don't know whether the money moved.

*(Attempt 6.)*

> Six. Another buyer takes the last cap between the signed checkout and the payment.
> Stock is re-read live, clean decline, nothing charged.

*(Open `demo/audit_chain.html` in a browser — it was just written by that run.)*

> Every run also writes this. A hundred and thirty-two audit rows, in order, each
> with the reason a person can read. Tick "show only decisions, payments and
> gates" — now it is fourteen lines and you can see the whole story: verified,
> declined, fell back, captured, once.
>
> It is self-contained. No CDN, no fonts, one inline script. It opens from a file
> on a machine with no network, because that is where somebody will open it.

---

## 2:30 — Interop, red team, benchmark

*(Terminal.)*

```bash
make interop
```

> This is the one I would look at if I were judging. That agent imports *nothing*
> from this project — not my models, not my signing code, not my client. It builds
> its mandate claims by hand from the field names in the spec and signs them with
> plain PyJWT.
>
> It just bought a pair of shoes. Fourteen checks passed on a mandate my own code
> never built. My agent working proves my code agrees with itself; this proves the
> gateway implements AP2 for somebody who has never read my source.

```bash
make redteam
```

> Twenty-one attacks. Forged signatures, `alg:none`, HMAC-with-the-public-key,
> payee substitution, nonce replay, a model that returns "DROP TABLE audit_log".
>
> Twenty-one blocked. And the bar is not "it returned an error" — it is zero
> rupees moved **and** zero orders created, because an attack you refuse after
> creating an order has already cost the merchant something. This exits non-zero
> if any attack lands, and there is a test that plants a fake breach to prove the
> report can come back red.

```bash
make bench
```

> And the number behind the whole design. Five hundred mandates, forty percent
> adversarial, each with a known expected outcome. **Zero false accepts.** p99
> under a millisecond.
>
> A model call on this path is three hundred to eight hundred milliseconds and a
> network dependency. That is four orders of magnitude — so "no LLM on the money
> path" is not a preference, it is the faster answer as well as the safe one.

---

## 3:30 — The measured result

*(Scroll to the reconciliation block and the final line.)*

> The audit trail is append-only and hash-chained. A hundred and thirty-two rows,
> chain intact, and every row carries one plain-English sentence explaining why.
> `tests/test_audit_chain.py` breaks that chain four ways — edits a payload, edits a
> reason, deletes a row, splices in a forgery — and catches all four, after first
> dropping the database triggers, because a tamper-evidence claim you haven't tried
> to break is a hope.
>
> Three independent records of the money — the payment rail, the spend ledger, the
> signed receipts — and the demo refuses to print a report unless all three agree.
>
> And the line:

*(This is on screen. Read it exactly as printed.)*

```
6 attempts · 4 paid · 1 human-denied · 1 recovered · ₹0 unauthorised · 6/6 explained
```

> **Six attempts. Four paid. One human-denied. One recovered. Zero rupees
> unauthorised. Six of six explained.**
>
> Every one of those is measured. The demo scripts two world events — the bank
> declining, and a buyer taking the last unit — and reads everything else back out
> of the modules. Break the rail in a test and `paid` goes to zero. Flip the
> simulated buyer to yes and it goes to five. If the number were hardcoded, those
> tests couldn't fail.

---

## 4:10 — Role mapping and what's next

*(Screen: the AP2 role table in README.md.)*

> Merchant and Merchant Payment Processor, implemented. Trusted Surface, implemented
> and non-agentic. Shopping Agent, simulated. Credential Provider, not implemented —
> and that's in LIMITATIONS.md, along with the fact that I use plain JWS rather than
> SD-JWT, that the audit chain detects tampering but can't prevent it, and that
> Razorpay has no server-side API to complete a payment, so the live path creates a
> link and polls rather than pretending otherwise.
>
> Next: SD-JWT selective disclosure, which is a change to one module. Real UPI
> Reserve Pay when NPCI's pilot opens — the mandate model already expresses
> reserve-then-capture. Multi-merchant routing. And an external anchor for the audit
> chain, to close the one tamper a self-contained chain can't catch.
>
> Five hundred and forty-two tests, ruff and mypy strict clean, and it runs offline
> in one command. CONFORMANCE.md scores every AP2 requirement honestly — twenty-four
> pass, eleven do not, and each one says why.

**Stop at 4:45.**

---

## Notes for recording

- Terminal at ~110 columns; the audit trail is formatted for it.
- `make demo` takes about a second. Pause and scroll rather than re-running.
- The one thing to *show*, not say: two `order_created` rows and one
  `payment_captured` row in attempt 4. That is the whole idempotency story in three
  lines of log.
- If the Trusted Surface page is awkward to reach live, `make serve` in a second
  terminal and open `http://127.0.0.1:8000` — it lists anything pending. Or
  screen-record it beforehand and cut it in.
- Do not read the failure-mode table aloud. Point at it, say "eight of them, each
  with a test asserting both the outcome and the audit row", move on.
