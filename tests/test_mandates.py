"""The JWS envelope and the AP2 content models.

This is the boundary every mandate crosses before any business rule runs, so the
tests here are mostly about what it *refuses*.
"""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from ap2_min.builders import open_checkout_mandate
from ap2_min.models import (
    AllowedMerchantsConstraint,
    AllowedPayeesConstraint,
    AmountRangeConstraint,
    BudgetConstraint,
    Cart,
    CartItem,
    CheckoutAmountCeilingConstraint,
    CheckoutMandateContents,
    PaymentMandateContents,
    inr,
    paise_to_inr_str,
)
from ap2_min.roles import ROLE_MERCHANT, ROLE_USER
from ap2_min.vct import (
    VCT_CHECKOUT_CLOSED,
    VCT_CHECKOUT_OPEN,
    VCT_PAYMENT_CLOSED,
    VCT_PAYMENT_OPEN,
)
from gateway.mandates import (
    KeyRing,
    MandateExpiredError,
    MandateMalformedError,
    MandateSignatureError,
    Signer,
    UnknownKeyError,
    UntrustedIssuerError,
    checkout_hash,
    decode_unverified,
    generate_keypair,
    load_contents,
    new_id,
    new_nonce,
    public_jwk,
    utcnow,
    verify_and_load,
    verify_jws,
)

from .conftest import make_signer

# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1299, 129900), ("1299", 129900), ("1299.50", 129950), ("0.05", 5), (0, 0), ("1,299", 129900)],
)
def test_inr_converts_rupees_to_paise(value: int | str, expected: int) -> None:
    assert inr(value) == expected


def test_inr_rejects_sub_paise_precision() -> None:
    with pytest.raises(ValueError, match="2 decimal places"):
        inr("10.005")


def test_paise_render_with_indian_grouping() -> None:
    assert paise_to_inr_str(129900) == "1,299.00"
    assert paise_to_inr_str(5) == "0.05"


# ---------------------------------------------------------------------------
# Content model invariants
# ---------------------------------------------------------------------------


def _cart(total_paise: int = 129900) -> Cart:
    return Cart(
        cart_id="cart_1",
        merchant_id="m_stridefit",
        merchant_name="StrideFit Sportswear",
        items=[
            CartItem(
                sku="SF-RUN-001",
                name="Velocity Road Runner",
                qty=1,
                unit_price=total_paise,
                line_total=total_paise,
            )
        ],
        total=total_paise,
        ship_to_pincode="560001",
    )


def test_cart_total_must_match_line_totals() -> None:
    with pytest.raises(ValueError, match="cart total"):
        Cart(
            cart_id="cart_1",
            merchant_id="m_stridefit",
            merchant_name="StrideFit Sportswear",
            items=[
                CartItem(sku="a", name="a", qty=2, unit_price=100, line_total=200),
            ],
            total=999,
            ship_to_pincode="560001",
        )


def test_line_total_must_match_qty_times_price() -> None:
    with pytest.raises(ValueError, match="line_total"):
        CartItem(sku="a", name="a", qty=2, unit_price=100, line_total=150)


def test_closed_checkout_mandate_requires_a_cart() -> None:
    with pytest.raises(ValueError, match="must carry a cart"):
        CheckoutMandateContents(vct=VCT_CHECKOUT_CLOSED, mandate_id="cm_1")


def test_open_checkout_mandate_must_not_carry_a_cart() -> None:
    with pytest.raises(ValueError, match="must not carry a cart"):
        CheckoutMandateContents(
            vct=VCT_CHECKOUT_OPEN,
            mandate_id="cm_1",
            cart=_cart(),
            constraints=[AllowedMerchantsConstraint(allowed=["m_stridefit"])],
        )


def test_open_checkout_mandate_requires_constraints() -> None:
    with pytest.raises(ValueError, match="at least one constraint"):
        CheckoutMandateContents(vct=VCT_CHECKOUT_OPEN, mandate_id="cm_1")


def test_open_checkout_mandate_requires_an_allowed_merchants_constraint() -> None:
    """Without it the authorisation covers every shop on the internet."""
    with pytest.raises(ValueError, match=r"checkout\.allowed_merchants"):
        CheckoutMandateContents(
            vct=VCT_CHECKOUT_OPEN,
            mandate_id="cm_1",
            constraints=[CheckoutAmountCeilingConstraint(max=inr(2000))],
        )


def test_checkout_constraints_use_the_specs_type_strings() -> None:
    """docs/ap2/checkout_mandate.md names this one; the other two are extensions."""
    mandate = open_checkout_mandate(
        allowed_merchants=["m_stridefit"], max_amount=inr(2000), ship_to_pincode="560001"
    )
    types = [c.type for c in mandate.constraints or []]
    assert types == [
        "checkout.allowed_merchants",
        "x-checkout.amount_ceiling",
        "x-checkout.ship_to",
    ]
    assert mandate.allowed_merchant_ids == ["m_stridefit"]
    assert mandate.max_amount == inr(2000)
    assert mandate.ship_to_pincode == "560001"


def test_duplicate_checkout_constraints_cannot_even_be_constructed() -> None:
    """Two constraints of one type is ambiguous authority.

    Caught at construction rather than at lookup, so a mandate that says "these
    merchants" twice with different lists cannot exist as an object, let alone be
    signed and presented.
    """
    with pytest.raises(ValueError, match="duplicate"):
        CheckoutMandateContents(
            vct=VCT_CHECKOUT_OPEN,
            mandate_id="cm_dupe",
            constraints=[
                AllowedMerchantsConstraint(allowed=["m_a"]),
                AllowedMerchantsConstraint(allowed=["m_b"]),
            ],
        )


def test_allowed_payees_accepts_the_specs_merchant_objects() -> None:
    """The spec's `allowed` array holds merchant objects; a bare id is shorthand."""
    from ap2_min.models import AllowedPayeesConstraint, Payee

    constraint = AllowedPayeesConstraint(
        allowed=["m_stridefit", {"id": "m_lumen", "name": "Lumen", "website": "https://lumen.test"}]
    )
    assert constraint.ids == ["m_stridefit", "m_lumen"]
    assert constraint.allowed[1] == Payee(id="m_lumen", name="Lumen", website="https://lumen.test")
    assert constraint.permits("m_lumen")
    assert not constraint.permits("m_lookalike")


def test_allowed_payees_matches_on_id_not_on_name() -> None:
    """A look-alike name is exactly what an allow-list exists to stop."""
    from ap2_min.models import AllowedPayeesConstraint

    constraint = AllowedPayeesConstraint(
        allowed=[{"id": "m_stridefit", "name": "StrideFit Sportswear"}]
    )
    assert not constraint.permits("StrideFit Sportswear")
    assert constraint.permits("m_stridefit")


def test_closed_payment_mandate_lists_every_missing_field() -> None:
    with pytest.raises(ValueError) as excinfo:
        PaymentMandateContents(vct=VCT_PAYMENT_CLOSED, mandate_id="pm_1", nonce=new_nonce())
    message = str(excinfo.value)
    for field in ("transaction_id", "payee", "payment_amount", "checkout_hash", "open_mandate_jws"):
        assert field in message


def test_open_payment_mandate_must_not_name_an_amount() -> None:
    with pytest.raises(ValueError, match="must not name an amount"):
        PaymentMandateContents(
            vct=VCT_PAYMENT_OPEN,
            mandate_id="pm_1",
            nonce=new_nonce(),
            constraints=[AmountRangeConstraint(max=inr(1500))],
            payment_amount=inr(100),
        )


def test_open_payment_mandate_requires_at_least_one_constraint() -> None:
    with pytest.raises(ValueError, match="at least one constraint"):
        PaymentMandateContents(vct=VCT_PAYMENT_OPEN, mandate_id="pm_1", nonce=new_nonce())


def test_amount_range_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="min must be <="):
        AmountRangeConstraint(min=inr(2000), max=inr(1000))


def test_mandates_are_frozen() -> None:
    cart = _cart()
    with pytest.raises(ValueError, match="frozen"):
        cart.total = 1  # type: ignore[misc]


def test_unknown_fields_are_rejected_not_ignored() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        AmountRangeConstraint(max=inr(1500), sneaky_override=True)  # type: ignore[call-arg]


def test_constraint_lookup_rejects_duplicates() -> None:
    mandate = PaymentMandateContents(
        vct=VCT_PAYMENT_OPEN,
        mandate_id="pm_dupe",
        nonce=new_nonce(),
        constraints=[AmountRangeConstraint(max=inr(1500)), AmountRangeConstraint(max=inr(99999))],
    )
    with pytest.raises(ValueError, match="duplicate"):
        mandate.constraint("payment.amount_range")


def test_constraint_lookup_returns_none_when_absent() -> None:
    mandate = PaymentMandateContents(
        vct=VCT_PAYMENT_OPEN,
        mandate_id="pm_1",
        nonce=new_nonce(),
        constraints=[AmountRangeConstraint(max=inr(1500))],
    )
    assert mandate.constraint("payment.budget") is None
    assert mandate.constraint("payment.amount_range") is not None


def test_constraint_union_discriminates_on_type() -> None:
    mandate = PaymentMandateContents(
        vct=VCT_PAYMENT_OPEN,
        mandate_id="pm_1",
        nonce=new_nonce(),
        constraints=[
            BudgetConstraint(max=inr(5000)),
            AllowedPayeesConstraint(allowed=["m_stridefit"]),
        ],
    )
    budget = mandate.constraint("payment.budget")
    assert isinstance(budget, BudgetConstraint)
    assert budget.max == 500000


# ---------------------------------------------------------------------------
# Signing and verification
# ---------------------------------------------------------------------------


def _open_payment(user_signer: Signer) -> str:
    contents = PaymentMandateContents(
        vct=VCT_PAYMENT_OPEN,
        mandate_id=new_id("pm"),
        nonce=new_nonce(),
        constraints=[AmountRangeConstraint(max=inr(1500)), BudgetConstraint(max=inr(5000))],
        cnf=user_signer.cnf,
    )
    return user_signer.sign(contents, ttl_seconds=3600)


def test_round_trip_sign_and_verify(user_signer: Signer, keyring: KeyRing) -> None:
    token = _open_payment(user_signer)
    contents, claims = verify_and_load(
        token, keyring, PaymentMandateContents, expected_role=ROLE_USER
    )
    assert contents.vct == VCT_PAYMENT_OPEN
    assert claims["iss"] == user_signer.kid
    assert claims["exp"] > claims["iat"]
    assert claims["jti"] == contents.mandate_id


def test_signature_tampering_is_detected(user_signer: Signer, keyring: KeyRing) -> None:
    token = _open_payment(user_signer)
    header, payload, signature = token.split(".")
    # Flip a character in the signature segment.
    flipped = "A" if signature[0] != "A" else "B"
    with pytest.raises(MandateSignatureError):
        verify_jws(f"{header}.{payload}.{flipped}{signature[1:]}", keyring)


def test_payload_tampering_is_detected(user_signer: Signer, keyring: KeyRing) -> None:
    """Re-encoding the payload with a raised ceiling must not verify."""
    import base64
    import json

    token = _open_payment(user_signer)
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["constraints"][0]["max"] = inr(999999)
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(MandateSignatureError):
        verify_jws(f"{header}.{forged}.{signature}", keyring)


def test_key_not_in_the_ring_is_rejected(keyring: KeyRing) -> None:
    stranger = make_signer("key_stranger", ROLE_USER)
    token = _open_payment(stranger)
    with pytest.raises(UnknownKeyError):
        verify_jws(token, keyring)


def test_valid_signature_by_the_wrong_role_is_still_rejected(
    merchant_signer: Signer, keyring: KeyRing
) -> None:
    """A merchant cannot mint the user's standing authorisation.

    The signature verifies. The authority does not. These are different failures
    and the second is the one that matters.
    """
    token = _open_payment(merchant_signer)
    verify_jws(token, keyring)  # signature alone is fine
    with pytest.raises(UntrustedIssuerError) as excinfo:
        verify_jws(token, keyring, expected_role=ROLE_USER)
    assert excinfo.value.code == "mandate.wrong_issuer"


def test_expired_mandate_is_rejected(user_signer: Signer, keyring: KeyRing) -> None:
    contents = PaymentMandateContents(
        vct=VCT_PAYMENT_OPEN,
        mandate_id=new_id("pm"),
        nonce=new_nonce(),
        constraints=[AmountRangeConstraint(max=inr(1500))],
    )
    stale = user_signer.sign(contents, ttl_seconds=60, now=utcnow() - timedelta(hours=2))
    with pytest.raises(MandateExpiredError):
        verify_jws(stale, keyring, leeway_seconds=0)


def test_alg_none_is_refused(keyring: KeyRing) -> None:
    """The classic JWT downgrade. ES256 is the only algorithm we accept."""
    token = jwt.encode(
        {"vct": VCT_PAYMENT_OPEN, "iss": "key_user_1", "iat": 1, "exp": 99999999999, "jti": "x"},
        key="",
        algorithm="none",
        headers={"kid": "key_user_1"},
    )
    with pytest.raises((MandateSignatureError, MandateMalformedError)):
        verify_jws(token, keyring)


def test_hmac_signed_token_using_the_public_key_is_refused(keyring: KeyRing) -> None:
    """Algorithm confusion: HS256 signed with the EC public key as the HMAC secret.

    Hand-rolled, because PyJWT refuses to *encode* this — but an attacker with a
    socket does not use PyJWT. What matters is that we refuse to *decode* it.
    """
    import base64
    import hashlib
    import hmac
    import json

    def b64u(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    public_pem = keyring.get("key_user_1").public_pem
    header = b64u(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "key_user_1"}).encode())
    payload = b64u(
        json.dumps(
            {"vct": VCT_PAYMENT_OPEN, "iss": "key_user_1", "iat": 1, "exp": 99999999999, "jti": "x"}
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    forged = b64u(hmac.new(public_pem.encode(), signing_input, hashlib.sha256).digest())

    with pytest.raises((MandateSignatureError, MandateMalformedError)):
        verify_jws(f"{header}.{payload}.{forged}", keyring)


def test_missing_kid_is_malformed(keyring: KeyRing) -> None:
    private_key, _ = generate_keypair()
    token = jwt.encode({"iss": "x", "iat": 1, "exp": 99999999999, "jti": "x"}, private_key, "ES256")
    with pytest.raises(MandateMalformedError, match="no kid"):
        verify_jws(token, keyring)


def test_iss_must_match_kid(keyring: KeyRing, user_signer: Signer) -> None:
    token = jwt.encode(
        {"iss": "someone_else", "iat": 1, "exp": 99999999999, "jti": "x"},
        user_signer.private_key,
        "ES256",
        headers={"kid": user_signer.kid},
    )
    with pytest.raises(MandateMalformedError, match="iss does not match"):
        verify_jws(token, keyring)


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b", "a.b.c.d", "....."])
def test_garbage_input_is_malformed_not_a_crash(garbage: str, keyring: KeyRing) -> None:
    with pytest.raises(MandateMalformedError):
        verify_jws(garbage, keyring)


def test_required_claims_are_enforced(user_signer: Signer, keyring: KeyRing) -> None:
    token = jwt.encode(
        {"iss": user_signer.kid, "iat": 1},  # no exp, no jti
        user_signer.private_key,
        "ES256",
        headers={"kid": user_signer.kid},
    )
    with pytest.raises(MandateMalformedError, match="required claim"):
        verify_jws(token, keyring)


def test_load_contents_rejects_a_payload_of_the_wrong_shape(
    user_signer: Signer, keyring: KeyRing
) -> None:
    """A validly signed token whose body is not a Payment Mandate."""
    token = user_signer.sign(
        open_checkout_mandate(
            allowed_merchants=["m_stridefit"], max_amount=inr(2000), ship_to_pincode="560001"
        ),
        ttl_seconds=60,
    )
    with pytest.raises(MandateMalformedError, match="PaymentMandateContents"):
        verify_and_load(token, keyring, PaymentMandateContents)


def test_signing_refuses_content_models_that_shadow_envelope_claims(user_signer: Signer) -> None:
    from pydantic import BaseModel

    class Sneaky(BaseModel):
        exp: int = 0

    with pytest.raises(ValueError, match="reserved JWS claims"):
        user_signer.sign(Sneaky(), ttl_seconds=60)


def test_keyring_refuses_silent_rotation(user_signer: Signer, keyring: KeyRing) -> None:
    _, other_pem = generate_keypair()
    with pytest.raises(ValueError, match="silently rotate"):
        keyring.register(user_signer.kid, ROLE_USER, other_pem)


def test_keyring_reregistration_of_the_same_key_is_a_no_op(
    user_signer: Signer, keyring: KeyRing
) -> None:
    keyring.register_signer(user_signer)
    assert keyring.get(user_signer.kid).role == ROLE_USER


# ---------------------------------------------------------------------------
# Hashing and inspection
# ---------------------------------------------------------------------------


def test_checkout_hash_is_stable_and_token_specific(
    merchant_signer: Signer,
) -> None:
    contents = CheckoutMandateContents(
        vct=VCT_CHECKOUT_CLOSED, mandate_id=new_id("cm"), cart=_cart()
    )
    token_a = merchant_signer.sign(contents, ttl_seconds=900)
    token_b = merchant_signer.sign(contents, ttl_seconds=900)
    assert checkout_hash(token_a) == checkout_hash(token_a)
    assert len(checkout_hash(token_a)) == 64
    # ECDSA is randomised, so two signatures over identical contents are two
    # different tokens — and therefore two different hashes. `payment.reference`
    # binds to one specific signed checkout, which is the point.
    assert token_a != token_b
    assert checkout_hash(token_a) != checkout_hash(token_b)


def test_decode_unverified_reads_claims_without_a_key(user_signer: Signer) -> None:
    token = _open_payment(user_signer)
    claims = decode_unverified(token)
    assert claims["vct"] == VCT_PAYMENT_OPEN
    assert claims["iss"] == user_signer.kid


def test_decode_unverified_on_garbage_raises_typed_error() -> None:
    with pytest.raises(MandateMalformedError):
        decode_unverified("nope")


def test_public_jwk_shape(user_signer: Signer) -> None:
    jwk = public_jwk(user_signer.private_key)
    assert jwk["kty"] == "EC"
    assert jwk["crv"] == "P-256"
    assert set(jwk) == {"kty", "crv", "x", "y"}
    assert "=" not in jwk["x"] and "=" not in jwk["y"]


def test_load_contents_strips_envelope_claims() -> None:
    claims = {
        "vct": VCT_CHECKOUT_OPEN,
        "mandate_id": "cm_1",
        "constraints": [
            {"type": "checkout.allowed_merchants", "allowed": [{"id": "m_stridefit"}]},
            {"type": "x-checkout.amount_ceiling", "max": inr(2000), "currency": "INR"},
        ],
        "iss": "key_user_1",
        "iat": 1,
        "exp": 2,
        "jti": "cm_1",
    }
    contents = load_contents(claims, CheckoutMandateContents)
    assert contents.max_amount == 200000
    assert contents.allowed_merchant_ids == ["m_stridefit"]


def test_merchant_signed_checkout_round_trips(merchant_signer: Signer, keyring: KeyRing) -> None:
    contents = CheckoutMandateContents(
        vct=VCT_CHECKOUT_CLOSED,
        mandate_id=new_id("cm"),
        cart=_cart(),
        delegate_chain=["a" * 64],
    )
    token = merchant_signer.sign(contents, ttl_seconds=900)
    loaded, _ = verify_and_load(
        token, keyring, CheckoutMandateContents, expected_role=ROLE_MERCHANT
    )
    assert loaded.cart is not None
    assert loaded.cart.total == 129900
    assert loaded.delegate_chain == ["a" * 64]
