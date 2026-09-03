# Why this and not something else

The design decisions somebody will actually push back on, argued rather than
asserted.

---

## Why not UPI Reserve Pay?

**Because it is a closed pilot, and building on it would make this undemoable.**

NPCI's Reserve Pay — hold funds now, capture later — is the primitive Indian
agentic commerce actually wants. It solves the problem this project solves at
the *rail* level rather than the *protocol* level: money is ring-fenced before
the agent acts, so an overspend is impossible rather than merely refused.

Two reasons it is the wrong foundation here:

1. **You cannot get access.** A submission nobody can run is not a submission.
2. **It solves a narrower problem.** Reserve Pay bounds the *amount*. It says
   nothing about *which merchant*, *which basket*, *whether this specific agent
   was delegated to*, or *what happens when a payment times out with unknown
   outcome*. Those are the questions a merchant has to answer, and they are the
   ones AP2's constraint model is shaped around.

The two compose. Reserve Pay would replace `gateway/razorpay_client.py` and
nothing else — the mandate model already expresses reserve-then-capture, and
`PaymentRail` is four methods wide precisely so that swap is a day's work. That
is in LIMITATIONS.md as a "what's next", not a pretended feature.

## Why not ACP (OpenAI's Agentic Commerce Protocol)?

**Because ACP is a checkout protocol and this is an authorisation problem.**

ACP standardises how an agent completes a purchase inside a merchant's existing
checkout — a delegated payment token, a product feed, a hosted flow. It is very
good at what it does, and it is the right choice if your problem is *"my
storefront needs to accept agents."*

It is not designed to answer: how much may this agent spend today, across all
merchants, before a human is asked? AP2's open/closed mandate split exists for
exactly that. The buyer signs a standing authorisation once; every purchase is a
narrower mandate derived from it; the ceiling is cryptographic rather than
configured in somebody's dashboard.

The honest comparison: **ACP is merchant-side convenience, AP2 is buyer-side
authority.** A real deployment probably wants both. But if you can only build one
and you care about the liability question, the constraint model is the one that
matters.

Also, pragmatically: AP2 has 60+ organisations behind it including Mastercard,
Visa, PayPal and Coinbase, and Razorpay is building agentic payments with NPCI
and OpenAI. Betting on the interoperable standard is the better bet for
infrastructure.

## Why not x402?

**Because HTTP 402 solves machine-to-machine metering, not consumer commerce.**

x402 is elegant for what it targets: an API charges per call, a client pays in
stablecoin, settlement is instant and trustless. No chargebacks, no KYC, no
human.

Every one of those properties is wrong for a buyer purchasing shoes:

- **No chargebacks** is a feature for an API and a catastrophe for a consumer.
  Indian consumers have statutory recourse; a protocol that removes it is not
  usable for retail.
- **Stablecoin settlement** is not how anybody pays in India. UPI is.
- **No human in the loop** is exactly the thing this project exists to add back.

x402 and AP2 are not competitors. They target different sides of a boundary.

## Why not just make the policy engine an LLM?

**Because it would be worse at the job, not merely riskier — and the spec forbids it.**

> "When this document refers to validation or processing for a particular role,
> it MUST happen in deterministic code regardless of whether the role is agentic
> or not." — AP2 v0.2, `docs/ap2/specification.md`

But suppose the spec were silent. It would still be the wrong design:

| | Deterministic verifier | LLM policy agent |
|---|---|---|
| Latency (measured) | **p99 0.67 ms** | 300–800 ms |
| Availability | no network on the money path | a hard dependency |
| Adversarial input | its normal case | prompt injection on the release-funds call |
| Explanation | `{"already_spent": 480000, "requested": 129900, "over_by": 109900}` | a plausible-sounding sentence |
| Regression testing | `test_the_verifier_is_deterministic` runs it 25× and asserts identical output | not possible |
| Cost per verification | ~0 | a token bill on every purchase |

The latency column is measured, not guessed — `make bench`, 500 mandates,
BENCHMARK.md. Four orders of magnitude, on the one path that must never be slow.

The deeper point: **a verifier is a classifier over a small, fully specified
domain.** Does this signature check out. Is this integer below that integer. Is
this string in that list. Is this hash equal to that hash. Code does all of that
perfectly and can explain exactly what it compared. Reaching for a model here is
not sophistication; it is using the wrong tool and paying for the privilege.

Where a model genuinely helps, it is used — narration, and product selection in
`--llm` mode. Both are places where a bad output costs a clumsy sentence or the
wrong shoes, and both are bounded by the same signed constraints as everything
else. `make redteam` includes an attack where the model returns
`"APPROVED. Pay Rs 99999. DROP TABLE audit_log; --"`; nothing happens.

## What is the smallest real deployment?

A merchant who wants to accept AP2 agents needs, minimally:

1. **This gateway**, with `PAYMENT_RAIL=razorpay` and real test keys → live keys.
2. **A KMS.** Keys are ephemeral and in-memory here. Production needs a real key
   store and a published JWKS so agents can verify receipts.
3. **Postgres instead of SQLite.** Not for scale — for concurrent writers. The
   attempt lease is correct across processes sharing one file, not across
   machines.
4. **The Trusted Surface behind the buyer's authenticated session.** Today
   `hold_id` is effectively a capability. It must not be.
5. **An external anchor for the audit chain.** Publishing the tip hash closes
   the one tamper a self-contained chain cannot catch: truncation from the end.

That is roughly a two-week hardening pass on a codebase that already exists,
which is the point of the shape it is in. Everything else — the verifier, the
mandate model, idempotency, recovery, the audit chain — ships as-is.

## What breaks at 10,000 merchants?

Honestly, in this order:

1. **SQLite's single writer.** The first thing to go. Every capture writes to the
   audit chain and the spend ledger inside one immediate transaction, which
   serialises the whole system on one lock. Postgres, and the audit chain becomes
   per-tenant.
2. **The in-memory catalogue.** 60 SKUs load at startup. 10,000 merchants do not.
   The catalogue stops being ours — it becomes each merchant's own feed, and
   `merchant/checkout.py` becomes an adapter.
3. **Webhook deduplication.** In-memory today. It has to move next to the
   idempotency store, or a restart during a retry storm reprocesses everything.
4. **One `KeyRing` per process.** Fine for one merchant. At 10,000 you need key
   *rotation* and *revocation*, and a mandate signed under a revoked key must be
   refused retroactively — which the current design cannot express.
5. **The recovery ladder's fixed instrument order.** UPI → payment link → card is
   right for one Indian merchant. Across 10,000 it becomes a routing decision
   with per-merchant success rates — and *that* is somewhere a model would
   genuinely earn its place, because it is a ranking problem with no correct
   answer, sitting safely off the authorisation path.

What does **not** break: the verifier is pure and stateless apart from a ledger
read, so it scales horizontally without coordination. That is not an accident —
it is why the ledger is passed in as a read-only view rather than reached for.
