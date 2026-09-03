# Limitations

What this is not. Stated plainly, because a project that overstates what it does is
harder to trust about the parts it gets right.

---

## Protocol

### Plain compact JWS, not SD-JWT

AP2 specifies SD-JWT for selective disclosure. An SD-JWT lets a holder reveal *some*
claims to one party and withhold the rest — a shopping agent could prove to a
processor that a payment is within budget without revealing the budget. We sign each
mandate as one ES256 compact JWS, so every verifier sees every claim.

**This is a privacy reduction, not an integrity reduction.** Signature binding,
tamper detection, key binding and constraint evaluation are all unaffected. The
consequence is that our Merchant Payment Processor learns the buyer's daily budget
when it only needs to know this transaction fits.

Closing it is a change to `gateway/mandates.py` and the signature checks in
`gateway/verify.py`, and nothing else — the seam was drawn there deliberately.
Marked with the comment `# AP2 uses SD-JWT for selective disclosure; we use plain
JWS.` at the top of that module.

Consequence in `payment.reference`: the spec says the hash algorithm is the SD-JWT's
`_sd_alg`, or sha-256 if undefined. We are not an SD-JWT, so `_sd_alg` is undefined
and it is sha-256.

### Single-hop delegation, not an arbitrary delegate chain

A closed mandate carries exactly one open mandate it derives from
(`open_mandate_jws`). AP2 permits a chain — user → orchestrator agent → sub-agent —
and a real multi-agent deployment would need it. Ours is one hop, which is the shape
a single shopping agent buying for one person actually has.

### No Credential Provider

AP2's fifth role verifies that an agent is entitled to a payment instrument, and
holds the instrument on the buyer's behalf. Not implemented. In this repository the
Merchant Payment Processor talks to Razorpay directly, so the buyer's instrument is
effectively Razorpay's test rail. A production deployment would want the CP role so
the instrument is held by a party that is neither the agent nor the merchant.

### The Checkout Mandate's shape, and two extension constraints

The spec defines two Checkout Mandate constraints. We implement
`checkout.allowed_merchants` exactly — same `type` string, same `allowed` array
of merchant objects. We do **not** implement `checkout.line_items`: the
merchant's own signed cart already pins every SKU and quantity, so a per-item
constraint would restate what the signature already guarantees.

A buyer's standing checkout authorisation also needs a spend ceiling and a
delivery address, and AP2 defines neither. Rather than smuggle those in as
untyped fields, they are two constraints under an `x-` prefix —
`x-checkout.amount_ceiling` and `x-checkout.ship_to` — each carrying the schema
and evaluation algorithm the spec requires of anyone defining a new constraint
type. The prefix means no future AP2 constraint can ever collide with them, and a
reader can tell at a glance which two are ours.

### `delegate_chain`, not `delegate_payload`

The spec's `delegate_payload` carries `{"...": digest}` entries, a shape that only
has meaning inside an SD-JWT. We sign plain JWS, so our `delegate_chain` is a
list of sha-256 hashes. The binding it expresses — this closed mandate was
assembled under that open one — is identical; the encoding is not.

### `payment.allowed_payees` entries match on `id`

The spec's `allowed` array holds merchant objects with a name and a website. Ours
holds the same objects plus a required stable `id`, and matching is performed on
`id` alone. Names are not identifiers, and a look-alike merchant name is exactly
the attack an allow-list exists to stop. A bare string is accepted as shorthand
for `{"id": ...}`.

### Three constraint types not implemented

The specification documents eight Payment Mandate constraints. Five are implemented
with their evaluation algorithms quoted verbatim. Three are not:

- `payment.agent_recurrence` — mandate reuse across a frequency window. Meaningful
  for subscriptions; our model is one closed mandate per transaction.
- `payment.allowed_payment_instruments` — we constrain the *method* through
  `config/policy.yaml`'s recovery ladder rather than through a signed constraint.
- `payment.allowed_pisps` — no Payment Initiation Service Provider in this flow.

### No A2A transport

AP2's reference samples use Google's Agent2Agent protocol. We expose the Merchant
role over MCP instead, because MCP is what the agent ecosystem this is aimed at
actually speaks. The mandate model is transport-agnostic; swapping the transport
touches `merchant/mcp_server.py` and nothing below it.

---

## The audit chain

### Detects tampering; does not prevent it

The chain catches an edited row, a deleted row, a reordered row and a spliced
forgery, and names the damaged row. Four tests break it four ways to prove that.

It cannot stop an attacker who can rewrite the *entire* table, because they can
recompute every hash. Preventing that needs an external anchor — publishing the tip
hash, a notary, append-only storage — and is not implemented.

### Truncation from the end is invisible

Deleting the last N rows leaves a chain that verifies perfectly. This is inherent to
a self-contained hash chain and is why `AuditLog.tip_hash()` exists and `GET /audit`
publishes it: anyone who records the tip today can detect truncation tomorrow.
Asserted honestly in `test_verify_chain_catches_a_truncated_tail_only_via_the_tip`.

---

## Payments

### Razorpay test mode only

`RazorpayRail` refuses in code to construct with a key id that is not `rzp_test_`.
No live path exists, deliberately.

### `complete_test_payment` on the real rail polls; it does not pay

**There is no Razorpay API that completes a payment on a customer's behalf.** A
payment is made by a human on Razorpay's hosted page. So the live implementation
creates a payment link, prints it, and polls `order.payments` until a terminal
payment appears or a deadline passes. You pay it with `success@razorpay` or
`failure@razorpay`.

We could have pretended otherwise. Inventing an API that completes payments
server-side would have made the live demo smoother and the code wrong.

### The UPI-only payment-link option is documented but unverified here

`create_upi_payment_link` passes `options.checkout.method.upi = "1"` to restrict the
hosted page to UPI. That field is documented by Razorpay, but it is the one thing on
the live path we have not been able to confirm against a real sandbox from this
machine. Everything else uses core, long-stable fields (`amount`, `currency`,
`receipt`, `notes`, `payment_capture`, `reference_id`). If that option behaves
differently, the link still works — it just offers more methods than intended.

### Webhooks are optional and the fallback is polling

Signature verification is implemented and tested (HMAC-SHA256 over the raw body,
constant-time compare, rejection written to the audit chain). But with no public URL
you do not need webhooks: the payment path already polls `order.payments`, which is
the same source of truth Razorpay uses to *build* the webhook. Webhooks are faster;
polling is sufficient. See `docs/RAZORPAY_TESTING.md`.

Webhooks are also only *information*. A verified webhook resolves a pending payment;
it can never authorise one.

### No real UPI Reserve Pay

NPCI's UPI Reserve Pay — reserve funds now, capture later — is the primitive agentic
commerce in India actually wants, and it is a closed pilot. Not implemented. The
mandate model already expresses reserve-then-capture; only
`gateway/razorpay_client.py` would change.

### INR only

`payment.amount_range` carries a currency and `check_currency` enforces a match, so
a cross-currency mandate is refused rather than silently mis-compared. But there is
no FX, no multi-currency ledger, and `inr()` is the only money constructor.

### No refunds, settlements, disputes or chargebacks

Out of scope. A production MPP needs all four.

---

## Scale and operations

### Webhook deduplication is in memory

Deliveries are deduplicated on `X-Razorpay-Event-Id`, but the set of seen ids
lives in the process. A restart forgets them, and a second process would not share
them. It belongs in the database beside the idempotency store. The blast radius is
small — a webhook is information and can never authorise a payment — but the gap
is real.

### Single-process idempotency lease

The attempt lease is a conditional `UPDATE` inside `BEGIN IMMEDIATE`, which
serialises correctly across processes sharing one SQLite file. It would not survive
a horizontally scaled deployment across machines — that needs a shared lease store.
The capture probe limits the blast radius (a second process would find the first
process's capture before creating a new order) but does not eliminate the race.

### SQLite

One file, one writer. Correct and inspectable; not a payments platform's database.

### The catalogue is synthetic and in memory

Sixty invented SKUs across three fictional merchants, with plausible INR prices. No
real product, business or price is represented. Stock lives in memory and resets on
restart — a demo that mutates its own fixtures is a demo you can only run once.

### Search is a keyword match

`Catalog.search` does substring matching, then filters. Search relevance is a place
where a language model would genuinely help and would be entirely safe, and we
deliberately did not use one: ranking shoes is not what this project is about, and a
deterministic catalogue keeps the demo reproducible.

### No authentication, authorisation, or rate limiting on the HTTP service

`make serve` exposes the Trusted Surface page and the webhook receiver with no login.
The webhook route is protected by signature verification; the approval page is not
protected at all. In production the approval page is behind the buyer's own
authenticated session, and the `hold_id` is not a capability.

### Ephemeral keys

Generated in memory at startup. No JWKS endpoint, no rotation, no revocation. The
`KeyRing` refuses to silently rotate a `kid` to a different key, which is the one
piece of rotation hygiene that is present.

---

## The agent

### It is a simulation, not a general shopping agent

`shopping_agent/agent.py` exists so there is something to verify against. Its
planning is a fixed list of goals in `--scripted` mode and a single
"pick a SKU from this list" call in `--llm` mode. It is not trying to be a good
shopper. The point of this project is what happens when an agent — good or bad —
presents a mandate.

### The `interleave` hook

`ShoppingAgent.attempt()` takes an optional callable that runs between the signed
checkout and the payment. It exists so a scenario can make a concurrent world event
happen inside that window deterministically (another buyer taking the last unit).
It injects an *event*, never an outcome — what follows is decided entirely by the
merchant's own re-check. It is still a test seam in production code, and a larger
system would model this with a real second actor instead.

### `GateView` is a seam, not a capability boundary

The agent is handed a narrowed object with one read-only method. Python cannot make
that a hard boundary — a determined caller can walk `__self__` off a bound method.
The *hard* boundaries are elsewhere and are tested: the MCP surface has no approval
tool, the agent's key is a shopping-agent key and cannot sign an open mandate, and
`decide()` is reachable over HTTP only by a form POST. `GateView` makes the intent
legible in the type signature so that crossing it has to be deliberate.

---

## Things deliberately out of scope

No A2A transport. No SD-JWT selective disclosure. No multi-hop delegation. No real
UPI Reserve Pay. No auth system. No production UI beyond the approval page. No
deployment. No Docker. INR only.
