# What is unusual here

Six things, each with the file that proves it. Every one is a command a reviewer
can run, not a claim in a README.

---

### 1. A third-party AP2 agent that imports none of this code — and it buys something

`scenarios/ap2_reference/agent.py` builds its mandate claims **by hand** from the
spec's field names and signs them with **plain PyJWT**. It imports nothing from
`ap2_min`, `gateway`, `merchant` or `shopping_agent` — a test enforces that with
an AST walk. It talks to the Merchant over a real MCP client.

Our own agent working proves our code is self-consistent. *This* proves the
gateway implements AP2 for somebody who has never read our source: a bug in our
signing helper cannot be cancelled out by a matching bug in our verification,
because the agent does not use our helper.

```
make interop  →  third-party AP2 agent purchase: COMPLETED
```

**Proof:** `scenarios/ap2_reference/transcript.md`, `tests/test_standout.py::test_the_reference_agent_imports_nothing_from_this_project`

---

### 2. A red team that could come back red

21 executable attacks — forged signatures, `alg:none`, HMAC/EC confusion,
self-issued authority, payee substitution, checkout-hash swap, nonce replay,
negative and overflow amounts, currency mismatch, prompt injection, a model that
returns `DROP TABLE audit_log`, webhook forgery.

The bar is not *"the gateway returned an error"*. It is **`charged == 0` AND
`orders == 0`**, because an attack refused *after* an order exists has already
cost the merchant something. `make redteam` exits non-zero if any attack
succeeds, and a test plants a fake breach to prove the report is capable of
failing.

```
21/21 blocked · Rs 0 unauthorised · 0 orders created
```

**Proof:** `REDTEAM.md`, `redteam/attacks.py`, `tests/test_standout.py::test_the_red_team_would_report_a_breach`

---

### 3. The architectural claim is measured, not argued

Every project says deterministic beats an LLM on the money path. This one has the
number: **500 mandates, 0 false accepts, p50 0.32 ms, p99 0.67 ms, ~3,400/sec**
single-threaded, against a population that is 40% adversarial across seven
refusal classes, each generated with a known expected outcome.

A model call on the same path is 300–800 ms and a network dependency. That is
four orders of magnitude, on the one path that must never be slow or unavailable
— and it makes "we did not use an LLM here" an engineering result rather than a
preference.

**Proof:** `BENCHMARK.md`, `bench/run.py`

---

### 4. Honest conformance, including eleven rows that are not green

`CONFORMANCE.md` maps every normative AP2 requirement to code and to test:
**24 PASS, 6 PARTIAL, 5 NOT IMPLEMENTED, 4 N/A** — with a paragraph on each gap
explaining why, including two places we deliberately deviate from the spec and
argue we are right (budget consumed on capture rather than approval; payees
matched on `id` rather than name).

An all-green conformance table from a weekend project is not credible. This one
is checkable in both directions.

**Proof:** `CONFORMANCE.md`, `tests/test_ap2_fidelity.py` (26 tests)

---

### 5. The hardest bug in payments, simulated and defended

Not "a payment failed" — **a payment timed out and had actually succeeded.** The
caller learns nothing; a naive retry charges twice. No real sandbox can be asked
to produce this on demand, so `FakeRail.timeout_after_capture` records the
capture and *then* raises the timeout.

The capture probe asks the rail about every order already created under the same
idempotency key before creating another. It settles inside the same recovery run.

```
test_a_timeout_that_actually_captured_never_double_charges
test_a_timeout_that_captured_is_settled_within_the_same_recovery_run
test_a_deferred_timeout_that_captured_is_caught_on_the_next_tick
test_recovery_never_double_charges_when_a_declined_attempt_secretly_captured
```

**Proof:** `tests/test_correctness.py`, `gateway/recovery.py`

---

### 6. Documentation drift is a build failure

`tests/test_docs.py` — 59 tests — checks that every internal link resolves, that
the report line is byte-identical across four documents *and* matches
`report.json`, that the documented budget, catalogue size, recovery cap, check
count (14) and tool count (7) match the code, that every backticked path and
`make` target exists, that every environment variable the code reads is
documented **and** every documented one has a comment, and that every test
`SECURITY.md` cites actually exists.

Three of those tests failed on first run and found real errors — including that
`VIDEO_SCRIPT.md` instructed the reader to read out a line it did not contain.

**Proof:** `tests/test_docs.py`

---

## The honest summary

542 tests. `ruff` clean, `mypy --strict` clean, zero suppressions added during
hardening. `make demo` runs offline from a fresh clone and prints the same line
every time — proved by sabotaging `socket.socket` so the process cannot open one.

Nine real bugs were found and fixed during an adversarial self-review, and
`VERIFICATION_REPORT.md` lists all nine, including two that were mistakes in my
own tests. The live Razorpay path is **correct-by-review, not
correct-by-observation** — nobody has watched a real order appear in a real
dashboard, and §5 of that report says so rather than implying otherwise.
