# What broke, and how it was fixed

The buildathon asks what broke during development and how it was resolved. This
is that, honestly: **sixteen real defects**, what each would have cost, and how it
was found.

The pattern worth naming up front: **the tests were green for every one of them.**
A suite tells you the code does what you wrote it to do. It cannot tell you what
you failed to imagine. Eleven of these were found by *running the system in a shape
nobody had tried* — restarting it, hammering it with threads, handing it a second
valid mandate — not by reading it and not by adding assertions to paths that
already passed.

Three of the sixteen are mistakes in my own tests. They are here because a
write-up that only reports the code's failures and not the author's is not an
honest one.

---

## The four that would have taken real money — or faked it

### 1. One checkout could be paid twice

**Severity: highest.** Reachable by a well-behaved agent, on the happy path.

Idempotency keys on `sha256(payment_mandate.id)`, so it recognises the *same*
mandate. A second, freshly signed mandate for an already-paid checkout has a
different id and a different nonce — and passes every single verifier check. Same
payee, same amount, within budget, correct checkout hash, fresh nonce. All
fourteen checks say ALLOW, correctly, because each of them is individually right.

```
first  payment: captured
second payment: captured          ← different mandate, same checkout
rail captured = ₹2,598.00 | orders = 2   ← for a ₹1,299 basket
```

Nothing in the system asserted that *a checkout is a single purchase*. Mandate-level
idempotency cannot express it, because the two mandates genuinely are different.

**Fixed** with a checkout-level guard. Placement mattered more than the code: it
sits **after** mandate idempotency, so re-presenting the original mandate still
returns its receipt, and **before** the verifier, so nothing reaches the rail. One
step earlier breaks the idempotent-replay path; one step later creates the order
before refusing. `test_a_deferred_payment_can_still_be_completed` pins that
boundary, because the deferred-retry path runs right next to it.

*Found by:* asking "what if the agent just signs another one?"

### 2. Settlement crashed *after* the money moved

Under concurrency, several checkouts each passed the pre-payment stock re-check
while stock was still positive, then all captured. `commit_stock` then called
`Catalog.decrement`, which raises:

```
OutOfStock: SF-RUN-001 has 0 left, needed 1
  ← raised AFTER the rail had taken the money
```

The buyer was charged. The merchant's code was in a traceback. The agent held no
receipt and no way to tell a decline from a crash. The worst possible ordering of
those three facts.

**Fixed** by splitting one primitive into two, which is the real lesson:

| | Runs | On shortfall |
|---|---|---|
| `decrement()` | **before** money moves | raises — refusing is correct |
| `take()` | **after** money moves | clamps, reports a shortfall — refusing is not an available answer |

An oversell is a *fulfilment* problem: a backorder or a refund. The payment was
authorised, verified and captured, and that part was right. So it is recorded as a
`merchant.stock_oversold` audit row naming the receipt, and the buyer still gets
their receipt. Clamping silently would have hidden a real inventory problem.

The window cannot be closed without holding a lock across the rail round trip,
which would be worse. So it is detected and reported rather than pretended away.

*Found by:* five threads on a `Barrier`, one unit of stock.

### 3. A spent mandate answered for somebody else's checkout

Present a mandate that settled checkout A against checkout B and it returned A's
receipt, with `status: captured`. No money moved twice — but the agent was told B
was paid when it wasn't, and a merchant acting on that ships goods against a
receipt belonging to a different order.

A false positive on *"did this get paid"* is a real loss, and a quieter one than a
double charge: nothing reconciles wrong, so nobody notices until the customer
does.

**Fixed:** the idempotent-replay path now checks the stored receipt's
`checkout_hash` against the checkout being presented. A mismatch is
`mandate.spent_on_another_checkout` with both hashes in the audit row, so an
operator can see which two orders were confused. Replay for the mandate's *own*
checkout is untouched — a test pins that, because the two paths sit one line
apart.

*Found by:* the fifth verification pass, asking what happens if a mandate is
pointed at the wrong basket.

### 4. Eight simultaneous submissions all charged

The stored receipt is what makes a duplicate submit safe — but at t=0 there is no
stored receipt yet. Eight concurrent presentations of one mandate all read "not
settled", and all charged.

**Fixed** with a database-backed attempt lease: a conditional `UPDATE` inside
`BEGIN IMMEDIATE`, which serialises across processes sharing the file. The lease
expires so a crashed holder cannot wedge a mandate forever, and a successor still
runs the capture probe before creating anything.

---

## The two that would have cost a reviewer's trust

### 5. `.env` was never read

Every document said to put Razorpay keys in `.env`. `make setup` created one. **No
code ever loaded it.** A reviewer following the instructions would have hit:

```
RuntimeError: PAYMENT_RAIL=razorpay needs RAZORPAY_KEY_ID and
RAZORPAY_KEY_SECRET in .env. See docs/RAZORPAY_TESTING.md.
```

— a traceback telling them to do the thing they had just done. The single live
check the submission asks its own author to perform could not have worked.

**Fixed** with `gateway/config.py`: a twenty-line loader, no dependency. A package
that reads a file full of API keys is a package worth not having, and writing it
myself made "never evaluates anything" testable rather than trusted —
`$(...)`, backticks and `${...}` all stay literal text, with a test for each.

*Found by:* actually running `make demo LIVE=1` instead of assuming it worked.

### 6. A receipt did not survive a restart

`test_receipts_are_long_lived` asserts a receipt is valid for over 300 days, and
the entire evidential claim of this project is that a third party can verify one
months later. But keys were generated fresh on every boot:

```
receipt still verifies after restart: NO
  → MandateSignatureError: signature does not verify
```

The signature was sound. The public half that would prove it no longer existed. A
receipt meant as months-long evidence was unverifiable within minutes.

**Fixed** with an opt-in `GATEWAY_KEYSTORE`. Ephemeral stays the default — a test
run or an offline demo has nothing to be evidence for, and writing key material
during either would be worse than useless — and `make serve` sets one. The file is
created `0600` and the gateway refuses to start if it is readable by anyone else.
A corrupt keystore is refused rather than silently regenerated, because
regenerating would quietly invalidate every receipt already issued.

*Found by:* building a gateway, killing it, and starting another one.

---

## The four correctness and protocol defects

### 7. A refused mandate killed the whole checkout — a denial of service

One malformed or over-limit presentation marked the checkout `declined`
permanently. Anyone who could reach `initiate_payment` with a bad token could kill
a stranger's cart, and a legitimate agent that fixed its own mistake could not
retry. **Fixed:** `status` describes the checkout, not the outcome of one
presentation.

### 8. A stock re-check failure did the same

The merchant's signed price guarantee still stood for its full window and the
re-check runs again on every attempt — so killing the checkout lost a sale for no
safety benefit at all.

### 9. Recovery retried failures that could never succeed

A rejected *request* — bad amount, order already paid, suspended account — cannot
be fixed by a different instrument. The playbook walked the whole ladder anyway,
failing identically twice more and creating two orders for nothing. **Fixed:**
`RailDeclined` carries `retryable`; Razorpay 400s are non-retryable. Retrying a
failure that cannot succeed is not resilience, it is a slower way to reach the
same answer while generating noise for whoever reads the audit trail.

### 10. Webhook replay was not defended

A valid signature proves a delivery came from Razorpay. It does not prove it has
not arrived before — and Razorpay retries on any non-2xx, so duplicates are
*normal*, not exceptional. Anyone who captured one valid body could replay it for
as long as the secret lived. **Fixed:** deduplicated on `X-Razorpay-Event-Id`,
**after** signature verification, so an unauthenticated caller cannot claim an
event id to suppress the genuine webhook.

---

## The two AP2 fidelity drifts

### 11. The open Checkout Mandate used ad-hoc fields

`docs/ap2/checkout_mandate.md` defines `checkout.allowed_merchants` as a *typed
constraint* in a `constraints` array. The code carried loose `allowed_merchants` /
`max_amount` / `ship_to_pincode` fields instead. **Fixed** to the spec's shape,
plus two extensions under an `x-` prefix for the spend ceiling and delivery
pincode — bounds a buyer needs and AP2 does not define. The spec permits new
constraints provided each has a unique type, a schema and an evaluation algorithm;
each has all three.

### 12. `payment.allowed_payees` held bare strings

The spec's `allowed` array holds merchant objects with a name and a website.
**Fixed** to carry the spec's shape plus a required stable `id`, with matching
performed on `id` alone — a look-alike merchant name is exactly the attack an
allow-list exists to stop.

Fidelity is now a test rather than a claim: `tests/test_ap2_fidelity.py` asserts
every `vct` string, every constraint type and its exact field set, and that no
constraint carries a field the spec does not define.

---

## The one that was a design hole, not a bug

### 13. The agent could reach the Trusted Surface

A test I wrote to assert the boundary *failed*, and it was right to. The agent was
handed the `SimulatedShopper` object directly, which put `.surface` — and through
it `decide()` — one attribute away from code whose entire safety story is that it
cannot approve its own payments.

**Fixed** with a `GateView` exposing one read-only method. Python cannot make that
a hard capability boundary and LIMITATIONS.md says so; the boundaries that
actually hold are tested separately — the MCP surface has no approval tool, the
agent's key cannot sign an open mandate, and `decide()` is reachable over HTTP
only by a form POST.

---

## The three that were my own mistakes

### 14. mypy was not strict, and was being actively blinded

A `follow_imports = "skip"` override covered pyjwt, fastmcp, anthropic and mcp —
**all four of which ship `py.typed`**. It silently turned their return values into
`Any`. Removing it and enabling `strict = true` surfaced nine real errors,
including genuinely unreachable code in `merchant/service.py` carrying a
`# pragma: no cover` that had been hiding the fact it was dead.

Fixed at source. No `type: ignore` was added to get there; two pre-existing ones
were removed by typing the code properly.

### 15. A leak detector that fired on English prose

A test meant to prove no audit row carries a compact JWS matched any string with
two dots and 120 characters — so it fired on the verifier's own explanation
sentence. A test that flags prose proves nothing about tokens. Rewritten to
require the `eyJ` header prefix and no whitespace, **plus a test of the detector
itself**, because an assertion nobody has checked against a real positive is not
an assertion.

### 16. The secret scanner's bait leaked into git history

I wrote a fake credential as a literal to prove the scanner catches one. That put
a credential-shaped string into git history, which the scanner then correctly
flagged — and the tempting "fix" is to widen the allowlist until the scanner is
useless. Instead the bait is now assembled at runtime and the offending commit was
amended out.

---

## What this changed about how the project is built

Three habits came out of the above, and they are visible in the code:

**Nothing on the settlement path may raise.** Once money has moved, the code that
runs next owes the buyer a receipt. `take()` versus `decrement()` is that rule
made structural rather than remembered.

**Guards are placed, not just written.** The settled-checkout guard is correct only
in one position out of three. The regression test names the two wrong ones.

**Every claim in the documentation is a test.** `tests/test_docs.py` — 59 tests —
checks that the report line is byte-identical across four documents and matches
`report.json`, that the documented budget and catalogue size and check count match
the code, that every backticked path and `make` target exists, and that every test
`SECURITY.md` cites is real. Three of those failed on first run and found real
errors, including a video script that instructed the reader to read out a line it
did not contain.

---

## What is still not proved

`make demo LIVE=1` has never been run against a real Razorpay sandbox — no test
credentials were available. The live path is **correct by review**, endpoint by
endpoint against the official API and the SDK's own resolved URLs
(`VERIFICATION_REPORT.md` §5), and **not correct by observation**.

Everything else — 570 tests, 21 red-team attacks blocked, 0 false accepts in 500
mandates, a deterministic demo, a third-party AP2 agent completing a purchase — is
proved against `FakeRail`, the deterministic test double behind the same
`PaymentRail` protocol the real Razorpay client implements.
