"""AP2 v0.2 fidelity, asserted value by value.

The specification says implementations "MUST match the exact `vct` string,
including the version suffix", and it fixes the `type` string and field names of
each constraint. A comment claiming we match is worth nothing; this file is the
claim in executable form, so drift breaks the build.

Source documents, all from github.com/google-agentic-commerce/AP2 (Apache-2.0):

    docs/ap2/specification.md
    docs/ap2/payment_mandate.md
    docs/ap2/checkout_mandate.md
    docs/ap2/flows.md

Where we knowingly diverge, the divergence is asserted here too — so it stays a
recorded decision rather than quietly becoming a bug. Each is cross-referenced
in LIMITATIONS.md.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from ap2_min import models, vct
from ap2_min.builders import open_checkout_mandate, open_payment_mandate
from ap2_min.models import (
    AllowedMerchantsConstraint,
    AllowedPayeesConstraint,
    AmountRangeConstraint,
    BudgetConstraint,
    ExecutionDateConstraint,
    ReferenceConstraint,
    inr,
)
from gateway.bootstrap import Gateway

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# vct claims — docs/ap2/specification.md
# ---------------------------------------------------------------------------

SPEC_VCT = {
    "checkout_open": "mandate.checkout.open.1",
    "checkout_closed": "mandate.checkout.1",
    "payment_open": "mandate.payment.open.1",
    "payment_closed": "mandate.payment.1",
}


def test_every_vct_string_matches_the_spec_exactly() -> None:
    """ "Implementations MUST match the exact `vct` string, including the version
    suffix." — docs/ap2/specification.md"""
    in_code = {
        "checkout_open": vct.VCT_CHECKOUT_OPEN,
        "checkout_closed": vct.VCT_CHECKOUT_CLOSED,
        "payment_open": vct.VCT_PAYMENT_OPEN,
        "payment_closed": vct.VCT_PAYMENT_CLOSED,
    }
    assert in_code == SPEC_VCT


def test_the_vct_literals_admit_no_other_value() -> None:
    """A `vct` of `mandate.payment.2` must not be constructible, let alone valid."""
    from typing import get_args

    assert set(get_args(vct.CheckoutVct)) == {
        SPEC_VCT["checkout_open"],
        SPEC_VCT["checkout_closed"],
    }
    assert set(get_args(vct.PaymentVct)) == {
        SPEC_VCT["payment_open"],
        SPEC_VCT["payment_closed"],
    }


def test_receipt_vcts_are_namespaced_so_they_cannot_collide_with_ap2() -> None:
    """The spec fixes no receipt vct, so ours must be unmistakably ours."""
    for value in (vct.VCT_CHECKOUT_RECEIPT, vct.VCT_PAYMENT_RECEIPT):
        assert value.startswith("receipt.")
        assert "razorpay" in value
        assert not value.startswith("mandate.")


# ---------------------------------------------------------------------------
# Payment Mandate constraints — docs/ap2/payment_mandate.md
#
# (type string, {required field names}) exactly as the specification states them.
# ---------------------------------------------------------------------------

SPEC_PAYMENT_CONSTRAINTS: list[tuple[type, str, set[str]]] = [
    (BudgetConstraint, "payment.budget", {"max", "currency"}),
    (AmountRangeConstraint, "payment.amount_range", {"min", "max", "currency"}),
    (AllowedPayeesConstraint, "payment.allowed_payees", {"allowed"}),
    (ExecutionDateConstraint, "payment.execution_date", {"not_before", "not_after"}),
    (ReferenceConstraint, "payment.reference", {"conditional_transaction_id"}),
]


@pytest.mark.parametrize(
    ("model", "spec_type", "spec_fields"),
    SPEC_PAYMENT_CONSTRAINTS,
    ids=[c[1] for c in SPEC_PAYMENT_CONSTRAINTS],
)
def test_payment_constraint_type_and_fields_match_the_spec(
    model: type, spec_type: str, spec_fields: set[str]
) -> None:
    fields = set(model.model_fields)  # type: ignore[attr-defined]
    assert model.model_fields["type"].default == spec_type  # type: ignore[attr-defined]
    assert spec_fields <= fields, f"{spec_type} is missing {spec_fields - fields}"
    extra = fields - spec_fields - {"type"}
    assert not extra, f"{spec_type} carries fields the spec does not define: {extra}"


def test_the_five_implemented_constraints_are_the_only_payment_constraints() -> None:
    from typing import get_args

    implemented = {
        c.model_fields["type"].default for c in get_args(get_args(models.PaymentConstraint)[0])
    }
    assert implemented == {t for _, t, _ in SPEC_PAYMENT_CONSTRAINTS}


def test_the_unimplemented_spec_constraints_are_named_in_limitations() -> None:
    """Three documented constraints are out of scope. Say so where a reader looks."""
    limitations = (REPO_ROOT / "LIMITATIONS.md").read_text(encoding="utf-8")
    for absent in (
        "payment.agent_recurrence",
        "payment.allowed_payment_instruments",
        "payment.allowed_pisps",
    ):
        assert absent in limitations, f"{absent} is not implemented and not documented"


def test_every_constraint_docstring_quotes_its_evaluation_algorithm() -> None:
    """The spec states an evaluation algorithm per constraint; each is quoted."""
    for model, spec_type, _ in SPEC_PAYMENT_CONSTRAINTS:
        doc = inspect.getdoc(model) or ""
        assert spec_type in doc, f"{spec_type} docstring does not name the constraint"
        assert "MUST" in doc, f"{spec_type} docstring does not quote the spec's algorithm"


# ---------------------------------------------------------------------------
# Checkout Mandate constraints — docs/ap2/checkout_mandate.md
# ---------------------------------------------------------------------------


def test_the_spec_checkout_constraint_matches_exactly() -> None:
    assert AllowedMerchantsConstraint.model_fields["type"].default == "checkout.allowed_merchants"
    assert set(AllowedMerchantsConstraint.model_fields) == {"type", "allowed"}


def test_our_checkout_extensions_are_unmistakably_extensions() -> None:
    """The spec permits new constraints; it requires them to be uniquely typed.

    An `x-` prefix means no future AP2 constraint can ever collide with ours, and
    a reader can tell at a glance which is which.
    """
    from typing import get_args

    types = {
        c.model_fields["type"].default for c in get_args(get_args(models.CheckoutConstraint)[0])
    }
    spec_owned = {t for t in types if not t.startswith("x-")}
    ours = {t for t in types if t.startswith("x-")}
    assert spec_owned == {"checkout.allowed_merchants"}
    assert ours == {"x-checkout.amount_ceiling", "x-checkout.ship_to"}


def test_each_extension_documents_a_schema_and_an_evaluation_algorithm() -> None:
    """What the spec requires of anyone defining a new constraint type."""
    for model in (models.CheckoutAmountCeilingConstraint, models.CheckoutShipToConstraint):
        doc = inspect.getdoc(model) or ""
        assert "EXTENSION" in doc
        assert "Schema:" in doc
        assert "Evaluation:" in doc


def test_an_open_checkout_mandate_is_built_as_a_typed_constraints_array() -> None:
    """The spec's shape, not ad-hoc fields."""
    mandate = open_checkout_mandate(
        allowed_merchants=["m_stridefit"], max_amount=inr(1500), ship_to_pincode="560001"
    )
    body = mandate.model_dump(mode="json")
    assert body["vct"] == SPEC_VCT["checkout_open"]
    assert isinstance(body["constraints"], list)
    assert body["constraints"][0]["type"] == "checkout.allowed_merchants"
    assert body["constraints"][0]["allowed"] == [
        {"id": "m_stridefit", "name": None, "website": None}
    ]
    assert "allowed_merchants" not in body, "no ad-hoc field survives"
    assert "max_amount" not in body


# ---------------------------------------------------------------------------
# Documented divergences — asserted, so they stay decisions
# ---------------------------------------------------------------------------


def test_amounts_are_integer_paise_not_the_w3c_float() -> None:
    """W3C PaymentCurrencyAmount types `value` as a float. Float money is a bug."""
    assert models.AmountRangeConstraint.model_fields["max"].annotation is int
    assert models.BudgetConstraint.model_fields["max"].annotation is int
    assert models.Cart.model_fields["total"].annotation is int
    assert inr("1299.50") == 129950


def test_the_delegate_chain_divergence_is_documented() -> None:
    """The spec's `delegate_payload` shape only has meaning inside an SD-JWT."""
    doc = models.CheckoutMandateContents.model_fields["delegate_chain"].description or ""
    assert "delegate_payload" in doc
    assert "SD-JWT" in doc
    limitations = (REPO_ROOT / "LIMITATIONS.md").read_text(encoding="utf-8")
    assert "SD-JWT" in limitations


def test_the_sd_jwt_divergence_is_flagged_in_the_module_that_makes_it() -> None:
    source = (REPO_ROOT / "gateway" / "mandates.py").read_text(encoding="utf-8")
    assert "AP2 uses SD-JWT for selective disclosure; we use plain JWS" in source


def test_the_reference_constraint_uses_sha_256_because_sd_alg_is_undefined() -> None:
    doc = inspect.getdoc(ReferenceConstraint) or ""
    assert "_sd_alg" in doc
    assert "sha-256" in doc


# ---------------------------------------------------------------------------
# The unresolved_constraint error — docs/ap2/flows.md
# ---------------------------------------------------------------------------


def test_the_unresolved_constraint_error_names_the_constraint_it_cannot_resolve(
    wired: Gateway,
) -> None:
    """flows.md: a Human-Not-Present flow becomes Human-Present when the Merchant
    returns an `unresolved_constraint` error and brings the user back in.

    The spec does not fix the error's field names, so ours are ours: `error` is
    the literal string the spec uses, and the rest is what an agent actually needs
    to act — which constraint, why, how much, and where to send the human.
    """
    cart = wired.merchant.assemble_cart([{"sku": "SF-RUN-004", "qty": 1}])["cart"]
    checkout = wired.merchant.create_checkout(cart["cart_id"])
    response = wired.merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)

    assert response["error"] == "unresolved_constraint"
    assert set(response) >= {
        "error",
        "constraint",
        "human_reason",
        "checkout_id",
        "amount",
        "currency",
        "hold_id",
        "approval_url",
    }
    assert response["constraint"] == "checkout.amount_exceeds_standing_limit"


def test_the_verifiers_unresolved_constraint_uses_the_same_error_string(
    wired: Gateway,
) -> None:
    """Both places that can raise the gate speak the same word."""
    from gateway.verify import Outcome, verify_payment_mandate

    decision = verify_payment_mandate(
        wired.open_payment_jws,
        wired.merchant_signer.sign(
            __import__("ap2_min.builders", fromlist=["x"]).closed_checkout_mandate(cart=_a_cart()),
            ttl_seconds=900,
        ),
        wired.ledger,
        keyring=wired.keyring,
    )
    assert decision.outcome is Outcome.UNRESOLVED_CONSTRAINT
    body = decision.error_response()
    assert body["error"] == "unresolved_constraint"
    assert body["constraint"]


def _a_cart() -> models.Cart:
    return models.Cart(
        cart_id="cart_fidelity",
        merchant_id="m_stridefit",
        merchant_name="StrideFit Sportswear",
        items=[
            models.CartItem(
                sku="SF-RUN-004",
                name="Marathon Elite Carbon",
                qty=1,
                unit_price=inr(4999),
                line_total=inr(4999),
            )
        ],
        total=inr(4999),
        ship_to_pincode="560001",
    )


def test_both_ap2_flows_are_reachable(wired: Gateway) -> None:
    """Human-Not-Present is the default; Human-Present is what the gate produces."""
    # Human Not Present: the buyer signed open mandates once, the agent proceeds.
    assert wired.open_checkout_contents.is_open
    assert wired.open_payment_contents.is_open

    # Human Present: approval produces user-signed CLOSED mandates.
    cart = wired.merchant.assemble_cart([{"sku": "SF-RUN-004", "qty": 1}])["cart"]
    checkout = wired.merchant.create_checkout(cart["cart_id"])
    held = wired.merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)
    approved = wired.trusted_surface.decide(held["hold_id"], approve=True)

    from ap2_min.roles import ROLE_USER
    from gateway.mandates import load_checkout_mandate, load_payment_mandate

    assert approved.checkout_mandate_jws is not None
    assert approved.payment_mandate_jws is not None
    closed_checkout, _ = load_checkout_mandate(
        approved.checkout_mandate_jws, wired.keyring, expected_role=ROLE_USER
    )
    closed_payment, _ = load_payment_mandate(
        approved.payment_mandate_jws, wired.keyring, expected_role=ROLE_USER
    )
    assert closed_checkout.vct == SPEC_VCT["checkout_closed"]
    assert closed_payment.vct == SPEC_VCT["payment_closed"]


# ---------------------------------------------------------------------------
# The non-agentic Trusted Surface — docs/ap2/specification.md
# ---------------------------------------------------------------------------


def test_the_trusted_surface_imports_no_language_model() -> None:
    """The spec requires this role to be non-agentic. Checked, not asserted.

    Checked over the parsed syntax tree rather than the raw text, because the
    module's own docstring says "no import path to llm/" and a substring search
    would flag that sentence. Names in prose are not calls.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "gateway" / "trusted_surface.py").read_text(encoding="utf-8"))

    imported: set[str] = set()
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)

    for forbidden in ("llm", "anthropic", "openai", "ReasonWriter"):
        assert forbidden not in imported, f"trusted_surface imports {forbidden}"
        assert forbidden not in referenced, f"trusted_surface calls into {forbidden}"


def test_the_architecture_document_quotes_the_spec_on_both_requirements() -> None:
    """A reviewer should find the actual sentences, with the file cited."""
    architecture = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "MUST happen in **deterministic code**" in architecture
    assert "docs/ap2/specification.md" in architecture
    assert "non-agentic" in architecture
    assert "Trusted Surface role is a UI surface that is trusted" in architecture


def test_the_open_payment_mandate_the_buyer_signs_uses_every_bound(
    wired: Gateway,
) -> None:
    """The shipped standing authorisation is not a toy: four constraints, all real."""
    types = [c.type for c in wired.open_payment_contents.constraints or []]
    assert set(types) == {
        "payment.amount_range",
        "payment.budget",
        "payment.allowed_payees",
        "payment.execution_date",
    }


def test_a_reference_pinned_mandate_round_trips_through_json() -> None:
    """All five constraints must survive serialisation with their spec names."""
    mandate = open_payment_mandate(
        budget=inr(5000),
        amount_min=inr(1),
        amount_max=inr(1500),
        allowed_payees=["m_stridefit"],
        not_before=None,
        not_after=None,
        pinned_checkout_hash="a" * 64,
    )
    body = mandate.model_dump(mode="json")
    types = [c["type"] for c in body["constraints"]]
    assert set(types) == {
        "payment.amount_range",
        "payment.budget",
        "payment.allowed_payees",
        "payment.reference",
    }
    reparsed = models.PaymentMandateContents.model_validate(body)
    assert reparsed == mandate
