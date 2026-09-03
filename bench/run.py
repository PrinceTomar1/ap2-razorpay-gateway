"""`make bench` — 500 synthetic mandates through the real verifier.

Two questions, both answered with numbers rather than adjectives:

1. **Is it correct under volume?** Every mandate is generated with a known
   expected outcome, so a FALSE ACCEPT — something that should have been refused
   and was not — is countable. That number must be zero. It is the only metric
   here that would make the project worthless if it were non-zero.

2. **Is it fast enough to be on the critical path?** p50/p95/p99 in
   milliseconds. The claim in ARCHITECTURE.md is that deterministic verification
   is *cheaper* than asking a model, not merely safer. This measures it.

Deterministic: a fixed seed, so two runs give the same distribution of cases.
Latency naturally varies; the correctness numbers do not.
"""

from __future__ import annotations

import random
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from ap2_min.builders import closed_payment_mandate
from ap2_min.models import inr
from ap2_min.roles import ROLE_SHOPPING_AGENT
from gateway.bootstrap import build_gateway
from gateway.db import MEMORY
from gateway.mandates import Signer, generate_keypair, utcnow
from gateway.verify import Outcome, verify_payment_mandate

REPORT = Path(__file__).resolve().parent.parent / "BENCHMARK.md"
SEED = 20260905
SAMPLES = 500


@dataclass
class Case:
    """One synthetic mandate and the outcome it is *supposed* to get."""

    kind: str
    token: str
    should_allow: bool


def build_cases(gateway: Any, count: int) -> list[Case]:
    """Generate a mixed population: mostly valid, with every refusal class present."""
    rng = random.Random(SEED)
    merchant = gateway.merchant
    cart = merchant.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    merchant.complete_checkout(checkout["checkout_id"], gateway.open_checkout_jws)
    checkout_hash = checkout["checkout_hash"]

    stranger_key, _ = generate_keypair()
    stranger = Signer(kid="key_bench_stranger", role=ROLE_SHOPPING_AGENT, private_key=stranger_key)

    def mandate(**kw: Any) -> str:
        now = kw.pop("now", utcnow())
        signer = kw.pop("signer", gateway.agent)
        contents = closed_payment_mandate(
            payee=kw.pop("payee", cart["merchant_id"]),
            payee_name=cart["merchant_name"],
            amount=kw.pop("amount", inr(rng.randint(1, 1500))),
            payment_instrument="upi",
            checkout_hash=kw.pop("checkout_hash", checkout_hash),
            open_mandate_jws=gateway.open_payment_jws,
            execution_date=now,
            **kw,
        )
        return str(signer.sign(contents, ttl_seconds=600, now=now))

    #: (kind, weight, should_allow, factory)
    population: list[tuple[str, int, bool, Any]] = [
        ("valid", 60, True, lambda: mandate()),
        ("over_amount_range", 8, False, lambda: mandate(amount=inr(rng.randint(1501, 90000)))),
        ("payee_not_allowed", 8, False, lambda: mandate(payee=f"m_rogue_{rng.randint(1, 99)}")),
        ("bad_signature", 6, False, lambda: _corrupt(mandate())),
        ("wrong_issuer", 5, False, lambda: mandate(signer=stranger)),
        ("expired", 5, False, lambda: mandate(now=utcnow() - timedelta(hours=3))),
        ("reference_mismatch", 4, False, lambda: mandate(checkout_hash="f" * 64)),
        ("malformed", 4, False, lambda: "not-a-jws-at-all"),
    ]

    kinds: list[tuple[str, bool, Any]] = []
    for kind, weight, allow, factory in population:
        kinds.extend([(kind, allow, factory)] * weight)

    cases: list[Case] = []
    for _ in range(count):
        kind, allow, factory = kinds[rng.randrange(len(kinds))]
        cases.append(Case(kind=kind, token=factory(), should_allow=allow))
    return cases


def _corrupt(token: str) -> str:
    header, payload, signature = token.split(".")
    return f"{header}.{payload}.{('A' if signature[0] != 'A' else 'B')}{signature[1:]}"


def main() -> None:
    print(f"  building {SAMPLES} synthetic mandates…")
    gateway = build_gateway(db_path=MEMORY, use_llm=False, sleep=lambda _s: None)
    try:
        cart = gateway.merchant.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}])["cart"]
        checkout = gateway.merchant.create_checkout(cart["cart_id"])
        gateway.merchant.complete_checkout(checkout["checkout_id"], gateway.open_checkout_jws)
        checkout_jws = checkout["checkout_mandate_jwt"]

        cases = build_cases(gateway, SAMPLES)
        print("  verifying…")

        latencies: list[float] = []
        outcomes: Counter[str] = Counter()
        by_code: Counter[str] = Counter()
        false_accepts: list[Case] = []
        false_rejects: list[Case] = []

        wall_start = time.perf_counter()
        for case in cases:
            started = time.perf_counter()
            decision = verify_payment_mandate(
                case.token, checkout_jws, gateway.ledger, keyring=gateway.keyring
            )
            latencies.append((time.perf_counter() - started) * 1000.0)

            allowed = decision.outcome is Outcome.ALLOW
            outcomes[decision.outcome.value] += 1
            if not allowed:
                by_code[decision.code or "—"] += 1
            if allowed and not case.should_allow:
                false_accepts.append(case)
            # A "valid" case can legitimately be refused once the budget is spent
            # or its nonce is reused, so a false reject is only counted when the
            # refusal is not one of those two exhaustion reasons.
            exhausted = {"payment.budget.exceeded", "payment.nonce.replayed"}
            if not allowed and case.should_allow and decision.code not in exhausted:
                false_rejects.append(case)
        wall = time.perf_counter() - wall_start

        latencies.sort()

        def p(quantile: float) -> float:
            """Nearest-rank percentile over the sorted latencies."""
            return latencies[min(int(len(latencies) * quantile), len(latencies) - 1)]

        report = _render(
            cases=cases,
            latencies=latencies,
            outcomes=outcomes,
            by_code=by_code,
            false_accepts=false_accepts,
            false_rejects=false_rejects,
            wall=wall,
            p50=p(0.50),
            p95=p(0.95),
            p99=p(0.99),
            chain_ok=gateway.audit.verify_chain().ok,
            captured=gateway.ledger.total_captured(),
        )
        REPORT.write_text(report, encoding="utf-8")

        print(f"\n  \033[1mFALSE ACCEPTS: {len(false_accepts)}\033[0m")
        print(f"  p50 {p(0.50):.3f} ms · p95 {p(0.95):.3f} ms · p99 {p(0.99):.3f} ms")
        print(f"  {SAMPLES / wall:,.0f} verifications/sec single-threaded")
        print(f"  wrote {REPORT.name}\n")
        if false_accepts:
            raise SystemExit(1)
    finally:
        gateway.close()


def _render(**k: Any) -> str:
    cases: list[Case] = k["cases"]
    kinds = Counter(c.kind for c in cases)
    lines = [
        "# Benchmark",
        "",
        f"**FALSE ACCEPTS: {len(k['false_accepts'])}**  ·  "
        f"**p50 {k['p50']:.3f} ms · p95 {k['p95']:.3f} ms · p99 {k['p99']:.3f} ms**  ·  "
        f"**{len(cases) / k['wall']:,.0f} verifications/sec**",
        "",
        f"{len(cases)} synthetic Payment Mandates through the real verifier, each generated",
        "with a known expected outcome so that correctness is countable rather than",
        "asserted. Regenerate with `make bench`.",
        "",
        "## The number that matters",
        "",
        "| | |",
        "|---|---|",
        f"| **False accepts** (should have been refused, was not) | **{len(k['false_accepts'])}** |",
        f"| False rejects (valid, refused for a reason other than budget/nonce exhaustion) | {len(k['false_rejects'])} |",
        f"| Audit chain intact after the run | {'yes' if k['chain_ok'] else 'NO'} |",
        f"| Money moved by the verifier | ₹{k['captured'] / 100:,.2f} (it cannot — it holds a read-only ledger view) |",
        "",
        "A single false accept would make this project worthless, which is why it is the",
        "first line of the report rather than a footnote. `make bench` exits non-zero if",
        "it is ever above zero.",
        "",
        "## Latency",
        "",
        "| Percentile | Time |",
        "|---|---|",
        f"| p50 | {k['p50']:.3f} ms |",
        f"| p95 | {k['p95']:.3f} ms |",
        f"| p99 | {k['p99']:.3f} ms |",
        f"| mean | {statistics.mean(k['latencies']):.3f} ms |",
        f"| max | {max(k['latencies']):.3f} ms |",
        "",
        f"Single-threaded throughput: **{len(cases) / k['wall']:,.0f} verifications/sec** "
        f"({k['wall']:.2f}s wall for {len(cases)}).",
        "",
        "Each verification does an ES256 signature check on the closed mandate, a second",
        "on the embedded open mandate, a P-256 key-binding comparison, and eleven further",
        "checks. The two elliptic-curve verifications dominate; everything else is integer",
        "and string comparison.",
        "",
        "**Why this matters for the design.** ARCHITECTURE.md argues that deterministic",
        "verification is not merely safer than asking a model — it is *cheaper*. A model",
        "call on this path would be 300–800 ms and a network dependency. The measured p99",
        f"here is {k['p99']:.3f} ms with no network at all. That is a difference of roughly",
        "four orders of magnitude, on the one path that must never be slow or unavailable.",
        "",
        "## Decisions by outcome",
        "",
        "| Outcome | Count |",
        "|---|---|",
    ]
    for outcome, n in k["outcomes"].most_common():
        lines.append(f"| `{outcome}` | {n} |")
    lines += ["", "## Refusals by code", "", "| Code | Count |", "|---|---|"]
    for code, n in k["by_code"].most_common():
        lines.append(f"| `{code}` | {n} |")
    lines += [
        "",
        "## Input population",
        "",
        "| Generated kind | Count | Should be allowed |",
        "|---|---|---|",
    ]
    for kind, n in kinds.most_common():
        allow = next(c.should_allow for c in cases if c.kind == kind)
        lines.append(f"| `{kind}` | {n} | {'yes' if allow else 'no'} |")
    lines += [
        "",
        "Roughly 60% valid, 40% adversarial across seven refusal classes, drawn with a",
        f"fixed seed ({SEED}) so the population is identical between runs.",
        "",
        "## What this does not measure",
        "",
        "- **Not end-to-end.** This is the verifier alone. A full purchase also does a",
        "  catalogue lookup, a stock re-check, SQLite writes and a rail round trip.",
        "- **Not concurrent.** Single-threaded, one process. SQLite's single writer is",
        "  the real ceiling in a full flow, and LIMITATIONS.md says so.",
        "- **Not a claim about production hardware.** Measured on the machine that ran",
        "  `make bench`; treat the ratio to a model call as the durable finding, not the",
        "  absolute numbers.",
        "",
        "Generated by `make bench` — do not edit by hand.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
