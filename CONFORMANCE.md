# AP2 conformance

Every normative requirement this project takes on, mapped to the code that
implements it and the test that proves it. **Honest, not all-green** — six rows
are PARTIAL and five are NOT IMPLEMENTED, and each says why.

Source: `docs/ap2/specification.md`, `payment_mandate.md`, `checkout_mandate.md`,
`flows.md` — github.com/google-agentic-commerce/AP2 (Apache-2.0).

Scope: the **Merchant** and **Merchant Payment Processor** roles, plus the
**Trusted Surface**. Requirements addressed to the Shopping Agent or Credential
Provider are marked N/A with a note.

| | Count |
|---|---|
| PASS | 24 |
| PARTIAL | 6 |
| NOT IMPLEMENTED | 5 |
| N/A (another role) | 4 |

---

## Mandate format

| # | Requirement | Status | Code | Test |
|---|---|---|---|---|
| M1 | `vct` MUST match the exact string including version suffix | **PASS** | `ap2_min/vct.py` — closed `Literal`s | `test_every_vct_string_matches_the_spec_exactly` |
| M2 | An unknown `vct` version MUST be refused | **PASS** | `ap2_min/models.py` | `test_a_closed_mandate_with_a_tampered_vct_is_rejected` |
| M3 | Mandates signed ES256 | **PASS** | `gateway/mandates.py` | `test_only_es256_is_accepted` (5 algorithms) |
| M4 | `cnf` key binding (RFC 7800) on open mandates | **PASS** | `check_key_binding` | `test_a_stolen_standing_authorisation_cannot_be_presented_by_another_agent` |
| M5 | A closed mandate references the open mandate it derives from | **PASS** | `open_mandate_jws` | `test_the_open_mandate_is_verified_independently` |
| M6 | SD-JWT with selective disclosure | **NOT IMPLEMENTED** | — | — |
| M7 | `delegate_payload` for the delegate chain | **PARTIAL** | `delegate_chain: list[str]` | `test_the_delegate_chain_divergence_is_documented` |
| M8 | Multi-hop delegation chains | **NOT IMPLEMENTED** | single hop only | — |

**M6.** Plain compact JWS. Every verifier sees every claim, so our processor
learns the buyer's daily budget when it only needs to know this payment fits. A
privacy reduction, not an integrity one — signature binding, tamper detection,
key binding and constraint evaluation are unaffected. Confined to
`gateway/mandates.py` by design.

**M7.** The spec's `delegate_payload` carries `{"...": digest}` entries, a shape
that only has meaning inside an SD-JWT. Ours is a list of sha-256 hashes. The
binding expressed is identical; the encoding is not.

**M8.** A closed mandate carries exactly one `open_mandate_jws`. A real
multi-agent deployment (user → orchestrator → sub-agent) would need the chain.

## Payment Mandate constraints

| # | Constraint | Status | Evaluation implemented | Test |
|---|---|---|---|---|
| C1 | `payment.budget` | **PASS** | requested + prior total ≤ `max`, added on approval | `test_budget_is_spent_plus_amount_and_the_ledger_moves_only_on_capture` |
| C2 | `payment.amount_range` | **PASS** | within `min`/`max` inclusive; currency must match | `test_amount_range_bounds_are_inclusive_exactly_as_the_spec_says` (6 cases) |
| C3 | `payment.allowed_payees` | **PASS** | `payee` MUST be present in `allowed` | `test_allowed_payees_matches_on_id_not_on_name` |
| C4 | `payment.execution_date` | **PASS** | ≥ `not_before`, ≤ `not_after` | `test_expiry_is_evaluated_in_utc_and_one_second_past_is_rejected` |
| C5 | `payment.reference` | **PASS** | hash matches the Checkout Mandate; sha-256 as `_sd_alg` is undefined | `test_a_mismatched_checkout_hash_is_denied_even_for_an_identical_cart` |
| C6 | `payment.agent_recurrence` | **NOT IMPLEMENTED** | — | — |
| C7 | `payment.allowed_payment_instruments` | **NOT IMPLEMENTED** | — | — |
| C8 | `payment.allowed_pisps` | **NOT IMPLEMENTED** | — | — |

**C1 nuance.** The spec says the amount is added "after approval". We add on
**capture**, not on ALLOW. A payment that was authorised and then declined moved
no money, and if declines consumed budget then anyone able to make our payments
fail could lock a buyer out of their own daily limit without taking a rupee.
Deliberate deviation; `test_a_decline_does_not_consume_budget`.

**C3 nuance.** The spec's `allowed` holds merchant objects with a name and
website. Ours holds the same objects plus a required stable `id`, and matching is
on `id` alone — a look-alike merchant name is exactly what an allow-list exists to
stop.

**C6–C8.** Recurrence is meaningful for subscriptions; our model is one closed
mandate per transaction. Instruments are constrained through the recovery ladder
in `config/policy.yaml` rather than a signed constraint. There is no PISP in this
flow.

## Checkout Mandate constraints

| # | Constraint | Status | Code | Test |
|---|---|---|---|---|
| K1 | `checkout.allowed_merchants` | **PASS** | `AllowedMerchantsConstraint` | `test_the_spec_checkout_constraint_matches_exactly` |
| K2 | `checkout.line_items` | **NOT IMPLEMENTED** | — | — |
| K3 | New constraint types MUST have a unique `type`, a schema, an evaluation algorithm | **PASS** | `x-checkout.amount_ceiling`, `x-checkout.ship_to` | `test_each_extension_documents_a_schema_and_an_evaluation_algorithm` |

**K2.** The merchant's own signed cart already pins every SKU and quantity, so a
per-item constraint would restate what the signature guarantees.

## Verification (Merchant / MPP)

| # | Requirement | Status | Code | Test |
|---|---|---|---|---|
| V1 | Validation MUST happen in **deterministic code** regardless of whether the role is agentic | **PASS** | `gateway/verify.py` | `test_no_language_model_on_the_money_path` + AST import-graph check |
| V2 | Merchant validates checkout integrity | **PASS** | `merchant/service.py` | `test_a_checkout_mandate_from_the_wrong_role_is_refused` |
| V3 | Merchant validates Shopping Agent approval | **PASS** | role-aware `KeyRing` | `test_no_role_but_the_user_can_sign_a_standing_authorisation` |
| V4 | MPP verifies credential authorisation before processing | **PASS** | `execute_payment` refuses without ALLOW | `test_execute_payment_cannot_be_reached_without_an_allow` |
| V5 | Every constraint evaluated per its stated algorithm | **PASS** | one pure function per check | `tests/test_verify.py` (40 tests) |
| V6 | Verification is reproducible | **PASS** | pure over inputs + read-only ledger view | `test_the_verifier_is_deterministic` (25 runs) |

## Flows

| # | Requirement | Status | Code | Test |
|---|---|---|---|---|
| F1 | Human-Not-Present: user approves open mandates, agent proceeds | **PASS** | `gateway/bootstrap.py` | `test_both_ap2_flows_are_reachable` |
| F2 | Human-Present: user approves **closed** mandates on a Trusted Surface | **PASS** | `gateway/trusted_surface.py` | `test_approval_mints_a_mandate_scoped_to_exactly_this_purchase` |
| F3 | Merchant returns `unresolved_constraint` to bring the user back in | **PASS** | `merchant/service.py` | `test_the_unresolved_constraint_error_names_the_constraint_it_cannot_resolve` |
| F4 | Trusted Surface MUST be non-agentic | **PASS** | no `llm` in the import graph | `test_the_trusted_surface_imports_no_language_model` |
| F5 | Trusted Surface obtains **informed** consent | **PARTIAL** | merchant-signed amount + verifier's own sentence, escaped | `test_the_page_shows_the_merchant_signed_amount` |
| F6 | `unresolved_constraint` field names | **PARTIAL** | spec fixes the error string, not the shape | `test_the_verifiers_unresolved_constraint_uses_the_same_error_string` |

**F5.** The page states its exact scope in plain English and shows only
merchant-signed numbers. "Informed" is ultimately a human-factors claim that no
test can settle; what is tested is that nothing on the page is model-generated
and everything is escaped.

**F6.** The spec names the error but does not fix its fields. Ours carries
`error`, `constraint`, `human_reason`, `checkout_id`, `amount`, `currency`,
`hold_id`, `approval_url` — what an agent needs to act.

## Transport and roles

| # | Requirement | Status | Note |
|---|---|---|---|
| T1 | A2A transport | **PARTIAL** | MCP instead. The mandate model is transport-agnostic; swapping touches `merchant/mcp_server.py` and nothing below it. `make interop` shows a third-party agent working over it. |
| T2 | Credential Provider role | **N/A** | Not implemented. Our MPP talks to Razorpay directly, so the instrument is effectively Razorpay's test rail. |
| T3 | Shopping Agent role | **N/A** | Simulated so there is something to verify against. Not a conformance claim. |
| T4 | Network/issuer receives the Payment Mandate | **N/A** | No network integration in test mode. |
| T5 | Requirements on the Credential Provider | **N/A** | Role not implemented. |

## Beyond the spec

Things the spec does not require that are implemented anyway, because a payments
system without them would be unsafe:

| Property | Code | Test |
|---|---|---|
| Idempotency on `sha256(mandate.id)` | `gateway/payments.py` | `test_failure_6_the_same_mandate_twice_returns_the_first_receipt` |
| Capture probe before any retry (timeout-that-captured) | `gateway/recovery.py` | `test_a_timeout_that_actually_captured_never_double_charges` |
| DB-backed attempt lease for simultaneous submits | `gateway/payments.py` | `test_concurrent_presentations_charge_once` |
| Tamper-evident audit chain | `gateway/audit.py` | six tamper tests |
| Bounded recovery with an explicit stopping rule | `gateway/recovery.py` | `tests/test_recovery.py` |
| Non-retryable failures are not retried | `gateway/recovery.py` | `test_recovery_does_not_retry_a_non_retryable_failure` |
| Webhook replay protection | `gateway/webhooks.py` | `test_a_replayed_webhook_is_answered_once` |

---

Re-derive any row: `make test`, then read the named test. Every claim here is a
file and a function, not a paragraph.
