# Decisions

Every choice made without asking, and why. Roughly in build order.

---

## Protocol and data model

**Vendored `ap2_min/` instead of `pip install ap2`.**
The `ap2` distribution on PyPI (0.1.1) is an unaffiliated third-party mirror by
another author, not Google's. Its model surface is the A2A/ADK sample shapes
(`CartMandate`, `IntentMandate`) rather than the v0.2 open/closed mandate model with
`vct` claims and typed constraints — which is exactly the part this project must be
faithful about — and it pulls in google-adk, google-genai and a2a-sdk as extras.
Google's own SDK is not published to PyPI. So `ap2_min/` transcribes the subset used
here directly from `docs/ap2/specification.md`, `payment_mandate.md` and
`checkout_mandate.md`, with `vct` strings and constraint `type` strings matching
exactly and each evaluation algorithm quoted in the docstring that implements it.

**Integer paise everywhere, not float, not `Decimal`.**
AP2 borrows W3C `PaymentCurrencyAmount`, which types `value` as a float. Float money
is a correctness bug in a payments system. `Decimal` would be correct too; integers
make a fractional unit *impossible* rather than merely discouraged, and
`1500 <= 1500` needs no thought about context or rounding mode. Rupees appear in two
places only — `config/policy.yaml` and `merchant/seed.json` — and are converted once
at load by `inr()`.

**Five of the eight documented Payment Mandate constraints.**
`payment.budget`, `payment.amount_range`, `payment.allowed_payees`,
`payment.execution_date`, `payment.reference` — the ones meaningful for a
single-merchant INR checkout. `payment.agent_recurrence`,
`payment.allowed_payment_instruments` and `payment.allowed_pisps` are recorded in
LIMITATIONS.md.

**`allowed_payees` carries the spec's merchant objects, and matches on `id`.**
The spec's `allowed` array holds objects with a name and a website. Ours holds the
same objects plus a required stable `id`, and matching is performed on `id` alone
— names are not identifiers, and a look-alike merchant name is precisely the attack
an allow-list exists to stop. A bare string is accepted as shorthand for
`{"id": ...}` so policy files stay readable. *(This originally used bare strings
and was corrected during the adversarial review — see the entry below.)*

**Single-hop delegation.** A closed mandate carries one `open_mandate_jws`, not a
chain. Multi-hop was explicitly out of scope.

**Content models are frozen and `extra="forbid"`.** A mandate is a signed statement;
being able to mutate one after signing is a footgun with no upside. Forbidding
unknown fields means an unexpected claim is a red flag rather than something
silently dropped — quietly ignoring a "harmless" extra key is how parser-differential
bugs start.

---

## Cryptography

**Plain compact JWS, not SD-JWT.** Recorded in LIMITATIONS.md and marked with the
comment the brief asked for at the top of `gateway/mandates.py`. It is a privacy
reduction, not an integrity one, and the seam is drawn so that swapping it touches
that module and the signature checks in `gateway/verify.py` and nothing else.

**Ephemeral in-memory keys, generated at startup.** No key material in the
repository to leak, and the demo stays self-contained. Production would use a KMS and
publish a JWKS. What the demo demonstrates — that a signature binds an amount to a
payee to a cart to a buyer — is unaffected by where the key came from.

**Keys carry a *role*, and verification checks it.** "Was this signed by the user?"
is a different question from "is this signature valid?", and both must hold. A
merchant key signing a buyer authorisation verifies perfectly and is still refused,
with a distinct code (`mandate.wrong_issuer`). This was not in the brief; it closes
an authority hole that a signature check alone leaves open.

**RFC 7800 `cnf` key binding, checked.** *Added beyond the brief.* The buyer's open
mandate names the agent's public key; the verifier compares it against whoever signed
the closed mandate. Without it, an open mandate that ever appears in a log line is
bearer authority. A mandate with no `cnf` is **rejected**, not waved through —
failing open on a missing security claim is how these checks stop meaning anything.

**Hash the compact JWS, not the decoded claims.** No canonicalisation question, and
it binds to one specific signature — so a re-signed mandate with identical contents
is a different hash, which is what `payment.reference` wants.

**A currency check.** *Added beyond the brief.* `1500 USD < 1500 INR` is true, and
without an explicit comparison every numeric bound below it would be meaningless.

---

## The verifier

**`DENY` vs `UNRESOLVED_CONSTRAINT` split on *how the agent behaved*, not on which
bound was hit.** An agent that recognises it is short of authority and presents its
open mandate gets `UNRESOLVED_CONSTRAINT` and a human. An agent that pushes a
well-formed closed mandate over the limit gets `DENY`. Without this split the gate is
a suggestion. Both paths are tested.

**The verifier never raises.** A forged mandate is an answer, not an exception, so no
caller on the money path writes a `try` block around a policy question.

**The verifier takes a read-only `LedgerView`.** It is a pure function of its inputs
plus that view, so it cannot record a spend as a side effect of deciding whether one
is allowed. Only `gateway/payments.py` holds the mutating `Ledger`.

**`keyring` is a fourth parameter to `verify_payment_mandate`.** The brief specified
`(payment_mandate_jwt, checkout_jwt, ledger)`. Signature verification needs a trust
store, and hiding it in a module global would make the function untestable in
isolation. Keyword-only, so the positional signature is unchanged.

**Checks short-circuit in a fixed order.** The reported reason is the reason it
stopped, and the order is stable, so a denial is reproducible.

---

## Payments and recovery

**Three rail error types, not one.** `RailDeclined` (a definite no), `RailTimeout`
(outcome unknown), `RailUnavailable` (nothing happened). The playbook branches on the
difference and the breaker counts only the last two. Flattening them is how a system
retries something that already succeeded.

**A DB-backed attempt lease.** *Not in the plan; a test found the gap.* Eight
simultaneous presentations of one mandate all read "no receipt yet" and all charged.
The stored receipt cannot help at t=0 — only serialisation can. A conditional
`UPDATE` inside `BEGIN IMMEDIATE`, which works across processes; it expires so a
crashed holder cannot wedge a mandate, and a successor still runs the capture probe.

**A capture probe before every new order.** Ask the rail about every order already
created under this idempotency key. If one captured, stop and return it. This is what
makes retry-after-timeout safe rather than merely bounded.

**Declines do not consume budget.** Only a capture calls `record_spend`. Otherwise
anyone who could make our payments fail could exhaust a buyer's daily limit without
taking a rupee.

**A deferral issues no receipt.** When the breaker trips, the idempotency record
stays `in_flight` and the same mandate is presentable next tick. Issuing a failure
receipt would have been simpler and would have thrown away a good mandate.

**Nonce *ownership*, not presence.** `nonce_owner()` returns which mandate burned it.
Same mandate → a retry, allowed. Different mandate → a replay, refused. A boolean
cannot distinguish those, and the distinction is what makes "retry next tick" work.

**Backoff indexes on the retry, not the attempt.** `backoff_for(0)` is the wait
before the *second* attempt. The first is never delayed.

**`backoff_base_seconds: 0.0` in the shipped policy.** So the offline demo and the
test suite never sit idle. Documented in `config/policy.yaml` with the value to use
in a real deployment.

**A failure gets a signed receipt.** An agent that asked for money and got silence
cannot tell "declined" from "lost in transit", and that ambiguity is how double
charges happen. `status: failed` with a `failure_code` is a contract.

**Receipts live a year.** Disputes are months later.

---

## Razorpay

**`RazorpayRail` refuses a non-`rzp_test_` key id, in code.** A live key here would
cost real money; that is not a README warning.

**`complete_test_payment` on the real rail creates a payment link and polls.**
There is no Razorpay API that completes a payment on a customer's behalf. Inventing
one would have made the live demo smoother and the code wrong. The user pays with
`success@razorpay` or `failure@razorpay`; the poller reads `order.payments`, which is
the same source of truth Razorpay uses to build the webhook.

**Only `options.checkout.method.upi` is used from the less-common API surface**, and
it is flagged in LIMITATIONS.md as the one field on the live path not confirmed
against a real sandbox from this machine. Everything else is core, long-stable
fields.

**`receipt` on the order carries the idempotency key.** An operator looking at the
Razorpay dashboard can trace any order back to the mandate that authorised it,
without access to our database.

**Webhook signature verified over the raw bytes, before parsing, with `compare_digest`.**
Re-serialising parsed JSON would hash a different byte string than Razorpay signed.
A bad signature returns 400 (so Razorpay retries) rather than a silent 200, and the
rejection is itself an audit row.

**`LIVE=1` runs attempts 1 and 4 only.** The rest of the batch is about behaviour a
real sandbox cannot be made to produce on demand.

---

## The audit chain

**`human_reason` is inside the chain hash.** The brief specified
`sha256(prev_hash + canonical(actor, event, payload, ts))`; this adds `human_reason`
to that body. Rewriting the explanation of a payment without touching its numbers is
exactly the tamper a dispute would turn on, and an audit trail where the prose is
freely editable is not much of an audit trail.

**Database triggers refuse `UPDATE` and `DELETE`.** *Beyond the brief.* Defence in
depth, and it makes the tamper tests stronger: they drop the triggers first, so they
are demonstrating that the chain catches determined tampering rather than casual
tampering.

**`Event` constants, not string literals.** A test asserting "an audit row exists for
this failure mode" and the code writing that row cannot drift apart.

**`verify_chain()` returns which row broke and how**, not a boolean. "The chain is
broken" is not actionable; "row 5 links to a hash the previous row doesn't produce,
so something was deleted" is.

**`render_log` folds runs of *passing* verifier checks.** Fourteen per purchase is
noise; a *failing* check is never folded, because that is the line a reader is
looking for. `--verbose` shows all.

---

## The LLM

**One interface, one method, two implementations.** An interface that small is one
you can swap, stub or delete without touching anything else — which is what makes
"no LLM on the money path" checkable rather than aspirational: there is exactly one
door, and the money path has no key to it.

**Template first, model second.** `reason()` computes the deterministic sentence
from the facts, then *optionally* asks a model to phrase it better. Any failure —
missing key, network, quota, an SDK exception we have never seen — falls back to the
template. `reason()` never raises. An audit row that failed to write because a model
was down would be worse than no narration at all.

**`LLM_PROVIDER` defaults to `fake`, and `PAYMENT_RAIL` defaults to `fake`.** The
dangerous default is the one that reaches for a credential and a network when nobody
asked it to.

**`FakeLLM` returns the draft unchanged** rather than inventing prose. In offline
mode the narration you read is honestly the template, not something pretending a
model wrote it.

**A model is used for product selection in `--llm` mode, and its answer is validated
against the candidate SKUs.** An unrecognised answer falls back to the deterministic
choice. Its output is constrained to a choice from a list the *merchant* produced.

**Catalogue search is a keyword match, not semantic.** This is the one place a model
would genuinely help and would be entirely safe, and we did not use one: ranking
shoes is not what this project is about, and a deterministic catalogue keeps the demo
reproducible. Recorded in LIMITATIONS.md so it reads as a decision rather than an
oversight.

---

## Merchant and agent

**A `merchant/service.py` layer under `merchant/mcp_server.py`.** The brief listed
only the MCP server. Keeping the logic in a service means the same code path runs
over MCP, over HTTP and from a test, and the MCP file stays a thin typed shell over
exactly the seven tools required.

**`create_checkout` returns a merchant-signed closed Checkout Mandate *and* an
unsigned open Checkout Mandate template.** The brief said "a merchant-signed Checkout
JWT + an open Checkout Mandate". Only the buyer can sign an open mandate, so what the
merchant can usefully return is the open mandate its cart *requires* — which lets the
agent see it needs to escalate rather than discovering it by being refused.

**The gate is raised at `complete_checkout`, not deferred to `initiate_payment`.**
If the basket is already outside the standing authorisation there is no point building
a Payment Mandate certain to be refused. Ask while the price guarantee is fresh.
`initiate_payment` still raises the gate for anything that reaches it.

**Stock is re-checked twice: before the verifier, and before every recovery attempt.**
Different windows. The first makes a sold-out cart a clean decline that burns no
mandate; the second catches a shelf emptying *during* a retry.

**Stock decrements on capture, never on checkout.** Reserving at checkout would be
defensible with a reservation TTL; decrementing before the money arrives means a
failed payment silently destroys inventory.

**One cart, one merchant.** One Payment Mandate names one payee, so a split basket
would make `allowed_payees` ambiguous.

**`GateView`.** *Added because a test caught it.* Passing `SimulatedShopper` straight
to the agent put `.surface` — and therefore `decide()` — one dot from code whose whole
safety story is that it cannot approve its own payments. The agent now gets a
one-method object. Python cannot make that a hard capability boundary and
LIMITATIONS.md says so; the hard boundaries are the MCP surface, the key roles, and
the HTTP route.

**The `interleave` hook on `attempt()`.** Modelling "another buyer takes the last
unit between checkout and payment" requires interleaving. The hook injects an
*event*; the outcome is decided by the merchant's re-check. It is still a test seam
in production code — noted in LIMITATIONS.md.

**Approval mints the narrowest possible mandate**, rather than raising the standing
limit: `min == max == this amount`, a budget equal to that amount, one merchant,
`payment.reference` pinned to this checkout, ten minutes. Getting this wrong is the
difference between a consent gate and a permission escalation.

**The Trusted Surface signs *as the buyer*.** It models the buyer's own device, which
is the only thing entitled to hold that key. Giving it a separate identity would be
pretending a website can consent on someone's behalf.

---

## Tooling

**FastMCP tools run with `run_in_thread=False`.** They are microseconds of in-process
work, and running them on the event loop keeps every SQLite handle on one thread.

**The demo drives the agent through a real in-process MCP client.** Real protocol,
real tool schemas, real serialisation boundary — the one a third-party AP2 agent
would hit — at no network cost.

**`BLE` enabled in ruff.** Every blind `except Exception` in this repository must be
justified with a `noqa` and a comment. There are two, both on the narration path,
both deliberate.

**`RUF001`–`RUF003` disabled.** This project renders money and status for humans:
rupee signs, em dashes, check marks. Every "ambiguous" character is a display glyph,
never an identifier.

**SQLite over Postgres.** The properties actually needed are durability, a
serialisable write path, and a single file a reviewer can open. A server would add
operational surface without adding a guarantee.

**Carts in memory, receipts on disk.** Losing an unconfirmed cart on restart is not a
correctness problem. Losing a receipt would be.

---

## Scenario

**The demo's six amounts were chosen so attempt 6 fails on stock and nothing else.**
₹1,299 + ₹899 + ₹1,199 + ₹699 = ₹4,096 of ₹5,000, leaving ₹904 — comfortably above
the ₹499 cap in attempt 6. If it were also over budget the demo would be ambiguous
about which guard fired, and there is a test
(`test_the_sold_out_goal_would_otherwise_have_been_affordable`) that keeps it that
way.

**Attempt 1 carries the hallucinated SKU.** Failure mode 7 folds into a purchase that
succeeds anyway, rather than needing an attempt of its own.

**The simulated buyer declines attempt 3.** A gate that is only ever tested by
approving is not tested.

---

# Adversarial review pass

A second pass, reviewing the project as somebody trying to reject it. Everything
below was decided during that review; the ones marked **found** were bugs the
checks uncovered rather than choices made up front.

## Tooling and configuration

**mypy runs in full `strict` mode.** *(found)* It was not. Worse, a
`follow_imports = "skip"` override was blinding it to pyjwt, fastmcp, anthropic
and mcp — all of which ship `py.typed`. Removing it and enabling `strict` surfaced
nine real errors. Third-party code is now trusted to describe itself, and
`razorpay` is the only module allowed to be `Any` because it is the only one that
ships no types.

**Deleted an unreachable branch rather than annotating it.** *(found)* Strict mypy
found dead code in `merchant/service.py` guarded by `# pragma: no cover` — a
comment that had been hiding the fact it could never run. `CheckoutVct` is a
two-value Literal and the other case returned above it.

**A dependency-free `.env` loader instead of `python-dotenv`.** *(found — nothing
read `.env` at all.)* The whole job is twenty lines of parsing. A package that
reads a file full of API keys is a package worth not having, and writing it means
the "never evaluates anything" property is testable rather than trusted:
`$(...)`, backticks and `${...}` all stay literal.

**A real environment variable always beats the file.** Standard dotenv semantics,
and it is what lets `demo/batch.py --live` set the rail before the file is read,
and CI secrets beat a checked-out `.env`.

**A typed `ConfigurationError`, caught at every entry point.** "You have not set
your Razorpay keys" is not a bug, and a stack trace buries the one sentence that
tells the reader what to do. Prints the message, exits 2.

**The demo pins its own in-memory database.** *(found)* Once `.env` was honoured,
`GATEWAY_DB=run/gateway.db` made the demo persistent and a second run inherited
the first run's spend — the reconciliation check caught it with "receipts total
₹4,096 but the spend ledger says ₹121,802". A demo whose result depends on how
many times you have run it is not reproducible. `make serve` still persists.

**Tests pin safe environment values rather than deleting them.** *(found)* Deleting
`GATEWAY_DB` let `load_dotenv` fill it back in from the developer's own `.env`,
which is how three tests started sharing a file on disk. Pinning is the only
version that actually isolates.

## Correctness

**A refused mandate no longer changes checkout state.** *(found)* One malformed or
over-limit presentation used to mark the whole checkout `declined` — a lost sale,
and a denial of service for anyone who can reach `initiate_payment` with a bad
token. `status` describes the checkout, not the outcome of one presentation.

**A stock re-check failure no longer kills the checkout either.** *(found)* The
merchant's signed price guarantee still stands for its full window and the
re-check runs again on every subsequent attempt, so if the shelf is restocked in
time the sale can still complete — and if it is not, the same branch refuses
again. Marking it dead bought nothing.

**`RailDeclined` carries `retryable`.** A declined card is retryable; "this order
is already paid", "the amount is invalid", "the account is suspended" are not.
They are properties of the request, not the instrument, so the rest of the ladder
would fail identically twice more and create two orders for nothing. Razorpay 400s
are classified non-retryable.

**`FakeRail.timeout_after_capture`.** The money moves and the caller never hears
back — the classic double-charge hazard, and a real sandbox cannot be asked to
produce it on demand. Writing the test revealed the system does something better
than expected: the capture probe settles it *inside the same recovery run*, before
the fallback creates an order. The test asserts the stronger behaviour.

**A precise JWS detector in the audit-leak test.** *(found)* The first version
matched any string with two dots and 120 characters, which flagged the verifier's
own English explanation. A test that fires on prose proves nothing. It now
requires the `eyJ` header prefix and no whitespace, and there is a test of the
detector itself.

## AP2 fidelity

**The open Checkout Mandate uses the spec's typed `constraints` array.** *(found)*
It was carrying ad-hoc `allowed_merchants` / `max_amount` / `ship_to_pincode`
fields. `checkout.allowed_merchants` is now the spec's own constraint, exactly.

**Two extension constraints under an `x-` prefix.** AP2 defines no per-checkout
spend ceiling and no delivery address, and both are bounds a buyer reasonably
wants. The spec permits new constraints provided each has a unique `type`, a
schema and an evaluation algorithm; `x-checkout.amount_ceiling` and
`x-checkout.ship_to` have all three, and the prefix guarantees no future AP2
constraint can collide.

**`payment.allowed_payees` holds merchant objects, matched on `id`.** *(found)* The
spec's `allowed` array holds objects with a name and a website; ours was a list of
bare strings. It now carries the spec's shape plus a required stable `id`, and
matching is on `id` alone — a look-alike merchant name is exactly what an
allow-list exists to stop. A bare string is accepted as shorthand.

**`checkout.line_items` deliberately not implemented.** The merchant's signed cart
already pins every SKU and quantity; the constraint would restate what the
signature guarantees.

**Fidelity is a test, not a claim.** `tests/test_ap2_fidelity.py` asserts every
`vct` string, every constraint type and its exact field set, that no constraint
carries a field the spec does not define, that each docstring quotes the algorithm
it implements, and that every deliberate divergence is documented where a reader
will look.

## Security

**Webhook deduplication on `X-Razorpay-Event-Id`.** *(found)* A valid signature
proves a delivery came from Razorpay, not that it has not arrived before —
Razorpay retries on any non-2xx, so duplicates are normal, and anyone who captures
one body can replay it. Deduplication happens **after** signature verification, so
an unauthenticated caller cannot claim an event id to suppress the real webhook.

**A duplicate returns 200, not 400.** The delivery genuinely succeeded and
Razorpay should stop retrying. The response says `duplicate: true` and the audit
row says so too, because an operator should be able to tell a retry storm from
real traffic.

**A webhook with no event id is processed but recorded as un-deduplicable.** Older
integrations omit it. Making the gap visible beats silently dropping the delivery
or silently failing to dedupe it.

**A real secret scanner instead of the naive grep.** The grep the review specified
matches variable names, `.env.example` placeholders, the SDK's `rzp_errors` alias
and documentation prose — it cannot be empty for a project that names
`RAZORPAY_KEY_ID` at all. The scanner looks for credential-*shaped* values across
full history against an explicit allowlist, plus a test asserting every allowlist
entry is obviously fake, plus a test that a planted key is caught.

**The scanner's bait is assembled at runtime.** *(found)* Writing it as a literal
put a credential-shaped string into git history, which the scanner then correctly
flagged — and the usual "fix" for that is widening the allowlist until the scanner
is useless. The offending commit was amended out.

**`SECURITY.md` names what is deliberately not defended.** No auth on the HTTP
surface, no rate limiting, no CSRF token, in-memory keys, single-process
guarantees. Absence should not be mistaken for coverage.
