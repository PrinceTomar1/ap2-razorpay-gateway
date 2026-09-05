# Status

**Everything runs. Nothing is broken. Nothing was cut.**

Triage found the build already green; the only defect was the report line
printing `Rs 0` where the specification says `₹0`. Fixed, propagated, and pinned
by a test.

---

## Proof

```
$ make setup && make test
setup complete — Python 3.13.5
570 passed in 5.46s                      ← 0 skipped, 0 xfail

$ ruff check .
All checks passed!

$ mypy gateway merchant shopping_agent
Success: no issues found in 24 source files

$ make demo                              ← run 1
6 attempts · 4 paid · 1 human-denied · 1 recovered · ₹0 unauthorised · 6/6 explained
$ make demo                              ← run 2, identical
6 attempts · 4 paid · 1 human-denied · 1 recovered · ₹0 unauthorised · 6/6 explained

$ cp -r . /tmp/acg-final && cd /tmp/acg-final
$ rm -rf .venv run .git && cp .env.example .env && make setup && make demo
setup complete — Python 3.13.5
6 attempts · 4 paid · 1 human-denied · 1 recovered · ₹0 unauthorised · 6/6 explained

$ grep -rn "anthropic\|reason(" gateway/verify.py gateway/payments.py gateway/recovery.py
$ echo $?
1                                        ← empty

$ pytest tests/test_failure_modes.py     ← 8 modes, each asserting an audit row
34 passed
  mode 1 bank decline          2 passed
  mode 2 API timeout           3 passed
  mode 3 invalid mandate       7 passed
  mode 4 budget breach         2 passed
  mode 5 stock race            3 passed
  mode 6 duplicate submit      3 passed
  mode 7 hallucinated SKU      3 passed
  mode 8 out-of-band request   3 passed

$ pytest -k "idempot or charge_once"     ← exactly one charge on duplicate submit
29 passed
$ pytest -k "tamper"                     ← verify_chain() catches 6 tamper types
9 passed
$ pytest tests/test_docs.py              ← every doc claim checked against the code
59 passed
```

Three extra gates, already built and passing:

```
$ make redteam    21/21 blocked · ₹0 unauthorised · 0 orders created
$ make bench      FALSE ACCEPTS: 0 · p50 0.27ms · p99 0.34ms
$ make interop    ✓ third-party AP2 agent purchase: COMPLETED
```

## What was cut

**Nothing.** Everything listed in the brief works and is tested. The red-team,
benchmark and interop layers already existed and were passing, so removing them
would have deleted proved functionality for no gain.

## What is not proved

**The live Razorpay path.** `make demo LIVE=1` has never been run against a real
sandbox — no test credentials were available. It is correct-by-review, endpoint
by endpoint against the official API and the SDK's own resolved URLs
(VERIFICATION_REPORT.md §5), and **not** correct-by-observation. README and
LIMITATIONS.md both say so in plain sight.

Everything else is proved against `FakeRail`, which is what `make demo` and all
570 tests run against.

## Bugs found and fixed in this session

Three, all invisible to the previously-green 542-test suite because the tests
exercised the paths the code was written for:

1. **A receipt did not survive a restart.** Keys were regenerated on every boot,
   so a receipt claiming a year of validity became unverifiable within minutes.
   Fixed with an opt-in `GATEWAY_KEYSTORE` (0600, refuses to start if wider).
2. **Settlement crashed after the money moved.** Under concurrency `commit_stock`
   raised `OutOfStock` *after* capture — buyer charged, code in a traceback, no
   receipt. Split into `decrement()` (raises, runs before payment) and `take()`
   (clamps and reports, runs after).
3. **One checkout could be paid twice.** A second freshly-signed mandate for an
   already-paid checkout passed every verifier check and charged again. Fixed
   with a checkout-level guard placed after mandate idempotency and before the
   verifier.
