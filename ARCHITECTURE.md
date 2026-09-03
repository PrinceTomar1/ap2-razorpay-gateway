# Architecture

## 1. The roles

AP2 v0.2 defines five roles. What each one is *for* matters more than what it is
called, so here is each with the question it answers.

| Role | The question it answers | Here |
|---|---|---|
| **Shopping Agent** | "What should we buy?" | `shopping_agent/agent.py` — simulated, so there is something to verify against |
| **Merchant** | "Is this basket real, at this price, for this buyer?" | `merchant/` — implemented |
| **Merchant Payment Processor** | "May this money move, and did it?" | `gateway/verify.py`, `payments.py`, `recovery.py` — implemented |
| **Trusted Surface** | "Does the human agree?" | `gateway/trusted_surface.py` — implemented, non-agentic |
| **Credential Provider** | "Is this agent entitled to this instrument?" | Not implemented — see [LIMITATIONS.md](LIMITATIONS.md) |

The separation is not organisational tidiness. It is the reason a merchant cannot
mint the mandate authorising its own payment, an agent cannot approve its own
escalation, and a processor cannot decide what a buyer wanted. Each of those is a
test in this repository, not a convention.

## 2. The mandates

Four kinds, and the open/closed split is the heart of it.

|  | **Open** — standing authorisation | **Closed** — one transaction |
|---|---|---|
| **Checkout** | `mandate.checkout.open.1`<br>*user-signed*: a typed `constraints` array — `checkout.allowed_merchants` (the spec's own) plus `x-checkout.amount_ceiling` and `x-checkout.ship_to` (documented extensions), 24h | `mandate.checkout.1`<br>*merchant-signed*: this exact cart at this exact price, 15 min |
| **Payment** | `mandate.payment.open.1`<br>*user-signed*: a list of AP2 constraints, no amount | `mandate.payment.1`<br>*agent-signed*: this much, to them, for this checkout, now, 10 min |

Two properties do most of the work:

**A closed Payment Mandate embeds the open one it claims authority from.** Not a
reference to look up — the actual compact JWS, in `open_mandate_jws`. The verifier
extracts it, verifies it independently against the *user's* key, and evaluates the
buyer's constraints from that. The agent supplies the token but has no way to
influence what it says.

**The buyer's open mandate names the agent's key in `cnf`** (RFC 7800). So a copy of
that mandate in anyone else's hands is inert: `check_key_binding` compares the
public coordinates of whoever signed the closed mandate against the `cnf` JWK, and
refuses a mismatch. Without this, a standing authorisation that ever appears in a log
line is bearer authority.

Every amount is an `int` count of paise. There is no float money anywhere in this
system, because a float cannot represent 0.1 and a payments gateway that is off by a
paise is a payments gateway with a bug.

## 3. The request lifecycle, end to end

A ₹1,299 purchase, from goal to receipt. Numbered steps correspond to what you see
in `make demo`'s audit trail.

### Once, at startup

The buyer signs two mandates on their Trusted Surface (`gateway/bootstrap.py`):

```
open Checkout Mandate   checkout.allowed_merchants   m_stridefit, m_lumen, m_pixelbyte
                        x-checkout.amount_ceiling    ₹1,500
                        x-checkout.ship_to           560001
                        cnf = agent key

open Payment Mandate    payment.budget          ₹5,000
                        payment.amount_range    ₹1 – ₹1,500
                        payment.allowed_payees  the same three
                        payment.execution_date  now → +24h
                        cnf = agent key
```

The `x-` prefixed two are extensions: AP2 defines no per-checkout spend ceiling and
no delivery address, and the spec explicitly permits new constraint types provided
each has a unique `type`, a schema and an evaluation algorithm. Each has all three,
and the prefix means no future AP2 constraint can collide with them.

Everything the agent can ever do flows from exactly these two tokens.

### Per purchase

**1. Search.** `search_inventory("running", {category, max_price_inr, size})`. A
keyword match over the catalogue, sorted by price then SKU so the same query always
returns the same list. Read-only, signs nothing.

**2. Check.** `check_product(sku)`. A hallucinated SKU costs one dictionary lookup
and returns `product.not_found` — no cart, no signature, and the verifier is never
invoked. *(Failure mode 7.)*

**3. Assemble.** `assemble_cart([{sku, qty}])` (`merchant/checkout.py`). Validates every SKU, stock level and
serviceability, refuses a basket spanning two merchants (one Payment Mandate names
one payee), and **stamps the prices in**. Those stamped prices are what makes a
later price change detectable rather than invisible.

**4. Create checkout.** `create_checkout(cart_id)`. The merchant signs a closed
Checkout Mandate over this exact cart — its ES256 signature is the price and
availability guarantee, valid 15 minutes. It also returns the *open* Checkout Mandate
template the buyer's standing authorisation must satisfy, so the agent can see it
needs to escalate rather than discovering it by being refused.

**5. Complete checkout.** `complete_checkout(checkout_id, checkout_mandate_jwt)`.
The agent presents the buyer's open Checkout Mandate. The merchant checks it is
user-signed, unexpired, covers this merchant, covers this amount, ships to this
pincode — and re-checks stock. Outcome: a signed **Checkout Receipt**, or an
`unresolved_constraint` naming the specific constraint, with a `hold_id` and an
`approval_url`.

**6. Sign the payment.** The agent builds a closed Payment Mandate: payee, amount,
instrument, `checkout_hash = sha256(the merchant's signed checkout JWS)`,
`execution_date = now`, and the buyer's open Payment Mandate embedded. It signs with
its own key.

**7. Initiate payment.** `initiate_payment(checkout_id, payment_mandate_jwt)`. The
order of the first three steps is load-bearing:

1. **Envelope.** Signature, structure, trusted key, role. A malformed or forged
   mandate dies here with a typed code, before anything else runs. *(Failure mode 3.)*
2. **Idempotency.** Already settled? Return the original receipt — *before* the
   verifier, so a duplicate submit does not trip replay detection on its own nonce.
   *(Failure mode 6.)*
3. **Stock.** Re-read live. A sold-out cart is a clean decline that burns no mandate.
   *(Failure mode 5.)*

**8. Verify.** `verify_payment_mandate` runs fourteen checks in a fixed order,
short-circuiting on the first failure so the reason it reports is the reason it
stopped. Every check is written to the audit log with the numbers it compared.

**9. Act on the decision.**

- `UNRESOLVED_CONSTRAINT` → a Trusted Surface hold, and the agent is handed an
  approval URL. *(Failure mode 8.)*
- `DENY` → a structured refusal with the arithmetic. *(Failure mode 4.)*
- `ALLOW` → the recovery playbook.

**10. Pay, with bounded recovery.** At most three attempts down an ordered instrument
ladder, with a stock re-check and a capture probe before every one. *(Failure modes
1 and 2.)*

**11. Settle.** A capture decrements stock (never before), records the spend against
the open mandate's running total, and issues an **MPP-signed Payment Receipt**. A
terminal failure issues one too — `status: failed` with a `failure_code`, because an
agent that asked for money and got silence cannot tell "declined" from "lost in
transit", and that ambiguity is how double charges happen.

## 4. The trust model

### Keys

Four ES256 (P-256) keypairs, generated in memory at startup, registered in a
`KeyRing` by `kid` **and role**. Verification asks "was this signed by the *user*?",
not "is this signature valid?" — those are different questions and both must hold. A
merchant key signing something that must come from the buyer verifies perfectly and
is still refused, with a distinct error code (`mandate.wrong_issuer`).

In production these come from a KMS and the public halves are published as a JWKS.
Ephemeral keys keep the demo self-contained and mean there is no private key in this
repository to leak.

### What the envelope refuses

`alg: none`. HMAC-with-the-public-key algorithm confusion. A missing `kid`. An `iss`
that disagrees with the `kid`. A payload carrying an unknown field (`extra="forbid"`
— silently dropping a "harmless" extra claim is how a parser-differential bug
starts). A content model that tries to define its own `exp`. All tested.

### What the agent cannot do

- Sign an open mandate — only the buyer's key can, and only the Trusted Surface holds it.
- Approve its own escalation — it is handed a `GateView` with one read-only method,
  and the merchant's MCP surface has no approval tool at all.
- Raise its own limits — the constraints come from a token it cannot forge.
- Spend twice — `sha256(mandate.id)`, a capture probe, and an attempt lease.
- Reach the payment rail without an `ALLOW` — `execute_payment` raises rather than
  charges.

### What an operator with database access cannot do

Edit the audit trail undetectably. The table refuses `UPDATE` and `DELETE` via
triggers; drop the triggers and the hash chain still catches an edited payload, an
edited reason, a deleted row or a spliced forgery, and names the damaged row. The one
tamper it cannot catch alone is truncation from the end, which is why `tip_hash()`
exists and `/audit` publishes it. Stated plainly in [LIMITATIONS.md](LIMITATIONS.md).

## 5. Where we deliberately do NOT use an LLM

> "When this document refers to validation or processing for a particular role, it
> MUST happen in **deterministic code** regardless of whether the role is agentic or
> not."
>
> — AP2 v0.2, `docs/ap2/specification.md`

> "The Trusted Surface role is a UI surface that is trusted to get informed user
> consent for an Intent before creating a user-signed Mandate."
>
> — *ibid.*, on the role that must remain non-agentic

So this is not a stylistic preference. It is the specification. But it is also the
right call on its own merits, and the reasoning is worth being explicit about.

**A verifier is a classifier over a small, fully specified domain.** Does this
signature check out. Is this integer below that integer. Is this string in that list.
Is this hash equal to that hash. Code does all of that perfectly, in microseconds,
and can explain exactly what it compared. A model does it probabilistically, cannot
be audited after the fact, cannot be reasoned about under adversarial input, and adds
a network dependency to the one path that must never be unavailable.

Four specific reasons it would be worse here:

1. **Adversarial input is the normal case.** The verifier's job is to be handed
   hostile mandates. Prompt injection against a component that decides whether to
   release funds is not a risk to mitigate — it is a category error.
2. **"Explainable" would stop meaning anything.** Today an audit row says
   `{"already_spent": 480000, "requested": 129900, "budget": 500000, "over_by": 109900}`.
   A model's explanation is a plausible-sounding sentence that may or may not describe
   what actually happened.
3. **Determinism is the property under test.**
   `test_the_verifier_is_deterministic` runs the same mandate 25 times and asserts
   identical output. You cannot write that test against a model, which means you
   cannot regression-test your money path.
4. **Availability.** A model being slow or down would mean payments stop. Narration
   being down means the prose is templated, which nobody notices.

**Where a model *does* run, and why it is safe there:**

| Use | Module | What a bad output costs |
|---|---|---|
| Audit narration | `llm/reason.py` | A clumsy sentence. The template is computed first and used on any failure — `reason()` never raises. |
| Product selection (`--llm` mode) | `shopping_agent/agent.py` | The wrong shoes. The answer is validated against the SKUs the merchant returned, and an unrecognised answer falls back to the deterministic choice. It is still bounded by the same ₹1,500 cap, the same allow-list, the same budget. |

Both are off the money path by construction, and the boundary is enforced rather
than asserted:

```bash
grep -rn "anthropic\|llm\." gateway/verify.py gateway/payments.py gateway/recovery.py
# (no output)
```

`tests/test_failure_modes.py::test_no_language_model_on_the_money_path` runs exactly
that grep, and a second test walks the import graph in case someone reaches the same
place indirectly. `config/policy.yaml` lists the forbidden uses as data, and a test
checks the list has not been quietly edited.

## 6. Design decisions worth defending

**Integer paise, not `Decimal`, not float.** `Decimal` would be correct too, but
integers make it impossible to accidentally introduce a fractional unit, and
`1500 <= 1500` needs no thought about context or rounding mode.

**Three rail error types, not one.** `RailDeclined` (a definite no), `RailTimeout`
(outcome unknown), `RailUnavailable` (nothing happened). The recovery playbook
branches on the difference, and the circuit breaker counts only the second and third.
Flattening them is how a system ends up retrying something that already succeeded.

**And declines carry `retryable`.** A declined card is retryable — the buyer's UPI
might work. "This order is already paid", "the amount is invalid", "the merchant
account is suspended" are not: they are properties of the *request*, not of the
instrument, so walking the rest of the ladder is guaranteed to fail identically,
twice more, and create two orders for nothing. Razorpay 400s are classified
non-retryable. Retrying a failure that cannot succeed is not resilience; it is a
slower way to reach the same answer while generating noise for whoever reads the
audit trail.

**A deferral issues no receipt.** When the breaker trips, the idempotency record
stays `in_flight`, the nonce stays attributed to that mandate, and the *same* mandate
can be presented on the next tick — where the capture probe first asks the rail
whether the deferred attempt actually went through. Issuing a failure receipt would
have been easier and would have thrown away a good mandate.

**Nonce ownership, not nonce presence.** `nonce_owner(nonce)` returns which mandate
burned it. The same mandate re-presenting its own nonce is a retry; a *different*
mandate presenting it is a replay. A boolean cannot tell those apart, and the
difference is what makes "retry next tick" work.

**`human_reason` is inside the chain hash.** It would have been easier to hash only
the machine payload. But the "why" column is what a person reads in a dispute, and an
audit trail whose numbers are tamper-evident while the explanation beside them is
freely editable is not much of an audit trail.

**Declines do not consume budget.** Only a capture calls `record_spend`. If declines
ate budget, anyone who could make our payments fail could lock a buyer out of their
own daily limit without ever taking a rupee.

**A refused mandate does not kill the checkout.** `status` describes the checkout,
not the outcome of one presentation. An agent that presents a malformed or
over-limit mandate can present a correct one next; letting a single bad
presentation invalidate the checkout would lose a legitimate sale and hand anyone
who can reach `initiate_payment` a way to kill a stranger's cart. Found during
adversarial review — it used to mark the checkout `declined`.

**The MCP surface has seven tools and no eighth.** No tool adjusts a price, skips
verification, retries a payment, approves a hold or raises a limit. The bound is the
absence of a function, not a check inside one — and there is a test asserting the
absence.

## 7. Concurrency

Three read-modify-write sequences must be atomic, and each is:

- **Appending to the chain.** Read the tip, write the row — inside `BEGIN IMMEDIATE`,
  so two concurrent appends cannot fork the chain.
- **Claiming an idempotency key.** Same transaction discipline.
- **Taking the attempt lease.** A conditional `UPDATE ... WHERE lease_expires IS NULL
  OR lease_expires <= now` inside `BEGIN IMMEDIATE`. This one was added because a test
  found the gap: eight simultaneous presentations of one mandate all read "no receipt
  yet" and all charged. The stored receipt cannot save you at t=0; only serialisation
  can. The lease expires so a crashed holder cannot wedge a mandate, and a successor
  still runs the capture probe before creating anything.

Burning a nonce relies on the primary key rather than a read-then-write, for the same
reason.

A fourth, outside the money path: **webhook deduplication**. A valid signature
proves a delivery came from Razorpay, not that it has not arrived before — Razorpay
retries on any non-2xx, so duplicates are normal. Deliveries are keyed on
`X-Razorpay-Event-Id` and answered once, *after* signature verification, so an
unauthenticated caller cannot claim an event id to suppress the genuine webhook.

## 8. Storage

SQLite, stdlib only. The properties actually needed are durability, a serialisable
write path, and the ability to hand a reviewer a single file they can open. SQLite
has all three; a server would add operational surface without adding a guarantee.

`$GATEWAY_DB` selects the file, and `.env` is read by `gateway/config.py` — a
twenty-line parser rather than a dependency, because a package that reads a file
full of API keys is a package worth not having. It never overrides a real
environment variable and never evaluates anything, so a `.env` cannot execute code.

**The demo ignores `$GATEWAY_DB` and always starts empty.** It portrays one
buyer's day against a ₹5,000 budget; carrying yesterday's spend in would make the
numbers depend on how many times you had run it, and "run it twice, get the same
answer" is the whole claim. `make serve` still persists, because a gateway that
forgets its receipts on restart would be useless.

Carts and checkouts are in memory — they are ephemeral, and losing an unconfirmed
cart on a restart is not a correctness problem. Losing a receipt would be, so
receipts, the spend ledger, the nonce registry and the audit chain are all on disk.
