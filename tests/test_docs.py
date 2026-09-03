"""The documentation must agree with the code.

A README that was true when it was written and is false now is worse than no
README: it costs a reader trust in everything else the project says. These tests
pin the specific factual claims — counts, amounts, file names, command names — so
that drift breaks the build instead of quietly misleading somebody.

They deliberately check *facts*, not prose. Nobody should have to rewrite a test
to improve a sentence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ap2_min.models import paise_to_inr_str
from gateway.bootstrap import Gateway
from gateway.policy import load_policy
from merchant.checkout import Catalog

REPO_ROOT = Path(__file__).resolve().parent.parent

DOCS = [
    "README.md",
    "VERIFICATION_REPORT.md",
    "ARCHITECTURE.md",
    "LIMITATIONS.md",
    "DEMO.md",
    "DECISIONS.md",
    "SECURITY.md",
    "VIDEO_SCRIPT.md",
    "BUILD_REPORT.md",
    "docs/RAZORPAY_TESTING.md",
]

EXPECTED_LINE = (
    "6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained"
)


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", DOCS)
def test_every_referenced_document_exists(name: str) -> None:
    assert (REPO_ROOT / name).is_file(), f"{name} is referenced but missing"


@pytest.mark.parametrize("name", DOCS)
def test_no_document_is_a_stub(name: str) -> None:
    assert len(_read(name).splitlines()) > 40, f"{name} is too short to be useful"


def test_every_internal_link_resolves() -> None:
    """A broken link in a submission is a reviewer hitting a 404 on your behalf."""
    broken: list[str] = []
    for name in DOCS:
        source = REPO_ROOT / name
        for target in re.findall(r"\]\(([^)#:]+?)(?:#[^)]*)?\)", _read(name)):
            if target.startswith(("http", "mailto:")):
                continue
            resolved = (source.parent / target).resolve()
            if not resolved.exists():
                broken.append(f"{name} -> {target}")
    assert broken == [], f"broken internal links: {broken}"


# ---------------------------------------------------------------------------
# The headline number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["README.md", "DEMO.md", "VIDEO_SCRIPT.md", "BUILD_REPORT.md"])
def test_the_report_line_is_quoted_identically_everywhere(name: str) -> None:
    """One character of drift and the video reads out something the demo did not print."""
    assert EXPECTED_LINE in _read(name), f"{name} does not carry the exact report line"


def test_the_report_line_matches_what_the_demo_actually_wrote() -> None:
    report = json.loads((REPO_ROOT / "demo" / "report.json").read_text(encoding="utf-8"))
    assert report["line"] == EXPECTED_LINE


# ---------------------------------------------------------------------------
# Numbers that appear in prose
# ---------------------------------------------------------------------------


def test_the_documented_budget_matches_the_policy_file() -> None:
    standing = load_policy("config/policy.yaml").standing_authorisation
    readme = _read("README.md")
    assert (
        f"₹{paise_to_inr_str(standing.daily_budget).rstrip('0').rstrip('.')}"
        in readme.replace("₹5,000/day", "₹5,000")
        or "₹5,000" in readme
    )
    assert "₹1,500" in readme
    assert standing.daily_budget == 500000
    assert standing.per_txn_max == 150000


def test_the_documented_catalogue_size_is_real() -> None:
    catalog = Catalog()
    assert len(catalog.products) == 60
    assert len(catalog.merchants) == 3
    for name in ("README.md", "ARCHITECTURE.md", "DEMO.md"):
        text = _read(name)
        if "SKU" in text:
            assert "60" in text, f"{name} states a catalogue size that is not 60"


def test_the_documented_recovery_cap_matches_the_policy() -> None:
    assert load_policy("config/policy.yaml").recovery.max_attempts == 3
    assert "3 attempts" in _read("README.md") or "3-attempt" in _read("README.md")


def test_the_documented_check_count_matches_the_verifier(wired: Gateway) -> None:
    """README says "fourteen checks run on a clean purchase". Count them."""
    from gateway.verify import verify_payment_mandate

    from .test_failure_modes import confirmed_checkout, signed_payment

    checkout = confirmed_checkout(wired)
    decision = verify_payment_mandate(
        signed_payment(wired, checkout),
        checkout["checkout_mandate_jwt"],
        wired.ledger,
        keyring=wired.keyring,
    )
    assert len(decision.checks) == 14
    assert "Fourteen checks" in _read("README.md")


def test_the_documented_tool_count_matches_the_mcp_server() -> None:
    from merchant.mcp_server import INSTRUCTIONS

    readme = _read("README.md")
    assert "7 MCP tools" in readme or "seven" in readme.lower()
    # Every tool the instructions name must be one the agent can actually call.
    for tool in (
        "search_inventory",
        "check_product",
        "check_serviceability",
        "assemble_cart",
        "create_checkout",
        "complete_checkout",
        "initiate_payment",
    ):
        assert tool in INSTRUCTIONS, f"{tool} is not described to the agent"


def test_the_documented_test_count_is_not_wildly_stale() -> None:
    """Allowed to lag a little; not allowed to be fiction."""
    import subprocess
    import sys

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    match = re.search(r"(\d+) tests? collected", collected)
    actual = int(match.group(1)) if match else 0
    assert actual > 400, "collection failed or the suite shrank unexpectedly"

    claimed = re.search(r"make test\s+# (\d+) tests", _read("README.md"))
    assert claimed is not None, "README no longer states a test count"
    assert abs(int(claimed.group(1)) - actual) <= 30, (
        f"README claims {claimed.group(1)} tests, suite has {actual}"
    )


# ---------------------------------------------------------------------------
# Every file and command a document names must exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", DOCS)
def test_every_source_file_named_in_prose_exists(name: str) -> None:
    """Backticked paths like `gateway/verify.py` must be real."""
    missing: list[str] = []
    for path in re.findall(r"`((?:[a-z_0-9]+/)+[a-z_0-9]+\.(?:py|yaml|json|md|sh))`", _read(name)):
        # `docs/ap2/...` are paths in Google's AP2 repository, cited as sources.
        # They are deliberately not vendored here.
        if path.startswith("docs/ap2/"):
            continue
        if not (REPO_ROOT / path).exists():
            missing.append(path)
    assert missing == [], f"{name} names files that do not exist: {missing}"


@pytest.mark.parametrize("name", DOCS)
def test_every_make_target_named_in_prose_exists(name: str) -> None:
    makefile = _read("Makefile")
    targets = set(re.findall(r"^([a-z][a-z0-9_-]*):", makefile, re.M))
    # Only inside backticks or after a shell prompt — otherwise "make it clearer"
    # reads as a target called `it`.
    text = _read(name)
    named = set(re.findall(r"`make ([a-z][a-z0-9_-]*)[ `]", text + " "))
    named |= set(re.findall(r"^\$ make ([a-z][a-z0-9_-]*)", text, re.M))
    named |= set(re.findall(r"^make ([a-z][a-z0-9_-]*)$", text, re.M))
    # English verbs, not targets: "make a change", "make that claim".
    named -= {"a", "an", "the", "it", "that", "this", "our", "them", "one", "sure", "sense"}
    unknown = named - targets
    assert unknown == set(), f"{name} names make targets that do not exist: {unknown}"


def test_every_environment_variable_documented_is_one_the_code_reads() -> None:
    """And every variable the code reads is documented."""
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", _read(".env.example"), re.M))
    used: set[str] = set()
    for package in ("gateway", "merchant", "llm", "shopping_agent", "demo"):
        for path in (REPO_ROOT / package).rglob("*.py"):
            used.update(
                re.findall(r'environ\.get\(\s*"([A-Z][A-Z0-9_]+)"', path.read_text("utf-8"))
            )
            used.update(re.findall(r'environ\[\s*"([A-Z][A-Z0-9_]+)"', path.read_text("utf-8")))

    undocumented = used - documented
    assert undocumented == set(), f".env.example does not document: {undocumented}"


def test_the_env_example_documents_every_variable_with_a_comment() -> None:
    """A variable with no explanation is a variable nobody sets correctly."""
    lines = _read(".env.example").splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^[A-Z][A-Z0-9_]+=", line):
            preceding = "\n".join(lines[max(0, index - 8) : index])
            assert "#" in preceding, f"{line.split('=')[0]} has no explanatory comment"


# ---------------------------------------------------------------------------
# Claims that must not be made carelessly
# ---------------------------------------------------------------------------


def test_the_docs_do_not_claim_a_live_razorpay_run_happened() -> None:
    """Nobody has watched a real order appear in a real dashboard. Say so."""
    build_report = _read("BUILD_REPORT.md")
    assert "LIVE=1" in build_report
    assert any(
        phrase in build_report
        for phrase in ("not been", "could not", "have not", "untested", "Untested")
    ), "BUILD_REPORT must be explicit that the live path is unverified by the author"


def test_the_failure_mode_table_covers_every_documented_mode() -> None:
    readme = _read("README.md")
    for mode in (
        "Bank decline",
        "API drop",
        "Invalid mandate",
        "Budget breach",
        "Stock race",
        "Duplicate submit",
        "Hallucinated SKU",
        "Out-of-band request",
    ):
        assert mode in readme, f"README's failure table is missing {mode}"


def test_the_architecture_document_names_the_modules_it_diagrams() -> None:
    architecture = _read("ARCHITECTURE.md")
    for module in (
        "gateway/verify.py",
        "gateway/payments.py",
        "gateway/recovery.py",
        "gateway/trusted_surface.py",
        "merchant/checkout.py",
    ):
        assert module in architecture


def test_limitations_names_every_unimplemented_ap2_piece() -> None:
    limitations = _read("LIMITATIONS.md")
    for piece in ("Credential Provider", "SD-JWT", "A2A", "line_items", "Reserve Pay"):
        assert piece in limitations, f"LIMITATIONS.md does not mention {piece}"


def test_security_document_links_each_threat_to_a_real_test() -> None:
    """Every test named in SECURITY.md must actually exist in the suite."""
    named = set(re.findall(r"`(test_[a-z0-9_]+)`", _read("SECURITY.md")))
    assert len(named) > 25, "SECURITY.md should cite the tests that prove its claims"

    existing: set[str] = set()
    for path in (REPO_ROOT / "tests").glob("*.py"):
        existing.update(
            re.findall(r"^(?:async )?def (test_[a-z0-9_]+)", path.read_text("utf-8"), re.M)
        )

    missing = {n for n in named if n not in existing}
    assert missing == set(), f"SECURITY.md cites tests that do not exist: {sorted(missing)}"
