"""AP2 v0.2 mandate, constraint and receipt models.

Transcribed from docs/ap2/payment_mandate.md and docs/ap2/checkout_mandate.md.
Every constraint class below carries the spec's evaluation algorithm in its
docstring, verbatim where the spec states one, because gateway/verify.py
implements exactly those sentences and nothing more.

Money
-----
All amounts are ``int`` paise. ₹1,299.00 is ``129900``. There is no float money
anywhere in this system: floats cannot represent 0.1 exactly, and a payments
gateway that is off by a paise is a payments gateway with a bug. Use
:func:`inr` to build amounts from rupees and :func:`paise_to_inr_str` to render
them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ap2_min.vct import (
    VCT_CHECKOUT_RECEIPT,
    VCT_PAYMENT_RECEIPT,
    CheckoutVct,
    PaymentVct,
)

CURRENCY_INR = "INR"


def inr(rupees: int | str) -> int:
    """Convert whole or decimal rupees to integer paise.

    ``inr(1299)`` and ``inr("1299.50")`` give ``129900`` and ``129950``.
    Accepts ``str`` so config and JSON can carry exact decimals without ever
    round-tripping through a float.
    """
    text = str(rupees).strip().replace(",", "")
    if "." not in text:
        return int(text) * 100
    whole, _, frac = text.partition(".")
    if len(frac) > 2:
        raise ValueError(f"INR has at most 2 decimal places, got {rupees!r}")
    frac = frac.ljust(2, "0")
    sign = -1 if whole.startswith("-") else 1
    return sign * (abs(int(whole or 0)) * 100 + int(frac))


def paise_to_inr_str(paise: int) -> str:
    """Render paise as a human-facing rupee string, e.g. ``129900`` -> ``1,299.00``."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    return f"{sign}{whole:,}.{frac:02d}"


class _Frozen(BaseModel):
    """Mandate contents are immutable once built.

    A mandate is a signed statement. Being able to mutate one after signing is a
    footgun with no upside, so every content model here is frozen and rejects
    unknown fields — an unexpected key in a submitted mandate is a red flag, not
    something to silently ignore.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Payment Mandate constraints (docs/ap2/payment_mandate.md)
#
# The spec treats constraints as an extension point: "To define a new
# constraint, the following MUST be specified: A uniquely defined `type`. A
# Schema... The evaluation algorithm." We implement the five the specification
# documents that are meaningful for a single-merchant INR checkout. The three we
# do not implement (payment.agent_recurrence, payment.allowed_payment_instruments,
# payment.allowed_pisps) are listed in LIMITATIONS.md.
# ---------------------------------------------------------------------------


class Payee(_Frozen):
    """A merchant, as ``payment.allowed_payees`` and ``checkout.allowed_merchants``
    carry them.

    The spec's ``allowed`` arrays hold merchant objects with a name and a website.
    We keep that shape, and add a required stable ``id`` which is the field
    matching is actually performed on. Names are not identifiers, and a
    look-alike name is precisely the attack an allow-list exists to stop — so
    ``name`` and ``website`` are carried for display and provenance, and
    :func:`gateway.verify.check_payee_allowed` compares ``id``.

    A bare string is accepted as shorthand for ``Payee(id=...)`` so that policy
    files and tests stay readable.
    """

    id: str
    name: str | None = None
    website: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_a_bare_id(cls, value: Any) -> Any:
        return {"id": value} if isinstance(value, str) else value


class BudgetConstraint(_Frozen):
    """``payment.budget`` — a cumulative ceiling across many transactions.

    Spec evaluation algorithm: "the requested amount plus the total sum of
    amounts from previously closed Payment Mandates MUST be less than or equal
    to ``max``. After approval, the amount MUST be added to the accumulated
    total for future evaluation."

    The "accumulated total" lives in :class:`gateway.ledger.SpendLedger`, which
    is what makes this the one constraint that cannot be evaluated from the
    mandate alone.
    """

    type: Literal["payment.budget"] = "payment.budget"
    max: int = Field(..., ge=0, description="Cumulative ceiling, in paise.")
    currency: str = CURRENCY_INR


class AmountRangeConstraint(_Frozen):
    """``payment.amount_range`` — per-transaction floor and ceiling.

    Spec evaluation algorithm: "The ``payment_amount`` property of the Payment
    Mandate MUST be within the range defined by ``min`` and ``max``. The
    ``currency`` property of the Payment Mandate MUST match the ``currency``
    property of this constraint."
    """

    type: Literal["payment.amount_range"] = "payment.amount_range"
    min: int = Field(0, ge=0, description="Inclusive floor, in paise.")
    max: int = Field(..., ge=0, description="Inclusive ceiling, in paise.")
    currency: str = CURRENCY_INR

    @model_validator(mode="after")
    def _ordered(self) -> AmountRangeConstraint:
        if self.min > self.max:
            raise ValueError("amount_range.min must be <= amount_range.max")
        return self


class AllowedPayeesConstraint(_Frozen):
    """``payment.allowed_payees`` — an allow-list of merchants.

    Spec evaluation algorithm: "The ``payee`` property of the Payment Mandate
    MUST be present in the ``allowed`` array."

    Entries are :class:`Payee` objects, matching the spec's shape. Matching is on
    the stable ``id`` — see :class:`Payee` for why.
    """

    type: Literal["payment.allowed_payees"] = "payment.allowed_payees"
    allowed: list[Payee] = Field(..., min_length=1, description="Merchants that may be paid.")

    def permits(self, merchant_id: str) -> bool:
        """Spec: the mandate's ``payee`` MUST be present in ``allowed``."""
        return any(payee.id == merchant_id for payee in self.allowed)

    @property
    def ids(self) -> list[str]:
        return [payee.id for payee in self.allowed]


class ExecutionDateConstraint(_Frozen):
    """``payment.execution_date`` — a validity window.

    Spec evaluation algorithm: "The ``execution_date`` of the Payment Mandate
    MUST be later than or equal to ``not_before`` (if present) and earlier than
    or equal to ``not_after`` (if present)."
    """

    type: Literal["payment.execution_date"] = "payment.execution_date"
    not_before: datetime | None = None
    not_after: datetime | None = None


class ReferenceConstraint(_Frozen):
    """``payment.reference`` — binds this authorisation to one Checkout Mandate.

    Spec evaluation algorithm: "The Checkout Mandate for the approved order MUST
    contain an open Checkout Mandate with a matching hash in its delegate chain.
    The hash algorithm used MUST be the ``_sd_alg`` algorithm for the SD-JWT this
    constraint is in, or ``sha-256`` if undefined."

    We are not an SD-JWT, so ``_sd_alg`` is undefined and the algorithm is
    sha-256 — see :func:`gateway.mandates.checkout_hash`.
    """

    type: Literal["payment.reference"] = "payment.reference"
    conditional_transaction_id: str = Field(
        ..., description="Lowercase hex sha-256 of the bound Checkout Mandate JWS."
    )


# ---------------------------------------------------------------------------
# Checkout Mandate constraints (docs/ap2/checkout_mandate.md)
#
# The spec defines `checkout.allowed_merchants` and `checkout.line_items`. We
# implement the first exactly. We do not implement `checkout.line_items` — the
# merchant's own signed cart already pins every SKU and quantity, so a per-item
# constraint would restate it (LIMITATIONS.md).
#
# The two extension constraints below exist because a buyer's standing checkout
# authorisation needs a spend ceiling and a delivery address, and the spec
# defines neither. Adding them is explicitly permitted: "To define a new
# constraint, the following MUST be specified: A uniquely defined `type`. A
# Schema... The evaluation algorithm." Each is uniquely typed under an `x-`
# prefix so it can never be confused with a future AP2 constraint, and each
# carries its schema and evaluation algorithm below.
# ---------------------------------------------------------------------------


class AllowedMerchantsConstraint(_Frozen):
    """``checkout.allowed_merchants`` — which shops this authorisation covers.

    Spec constraint. Evaluation: the merchant of the cart being checked out MUST
    be present in the ``allowed`` array. Matching is on :class:`Payee` ``id``.
    """

    type: Literal["checkout.allowed_merchants"] = "checkout.allowed_merchants"
    allowed: list[Payee] = Field(..., min_length=1)

    def permits(self, merchant_id: str) -> bool:
        return any(payee.id == merchant_id for payee in self.allowed)

    @property
    def ids(self) -> list[str]:
        return [payee.id for payee in self.allowed]


class CheckoutAmountCeilingConstraint(_Frozen):
    """``x-checkout.amount_ceiling`` — EXTENSION, not an AP2 constraint.

    Schema: ``max`` (integer paise), ``currency`` (ISO 4217).
    Evaluation: the ``total`` of the cart being checked out MUST be less than or
    equal to ``max``, and the cart's currency MUST equal ``currency``.

    Exists because AP2 defines no per-checkout spend ceiling, and a standing
    checkout authorisation without one authorises a cart of any size.
    """

    type: Literal["x-checkout.amount_ceiling"] = "x-checkout.amount_ceiling"
    max: int = Field(..., ge=0, description="Inclusive ceiling, in paise.")
    currency: str = CURRENCY_INR


class CheckoutShipToConstraint(_Frozen):
    """``x-checkout.ship_to`` — EXTENSION, not an AP2 constraint.

    Schema: ``pincode`` (string).
    Evaluation: the ``ship_to_pincode`` of the cart being checked out MUST equal
    ``pincode``.

    Exists because "you may shop for me, but only ship to my home" is a bound a
    buyer reasonably wants and AP2 does not express.
    """

    type: Literal["x-checkout.ship_to"] = "x-checkout.ship_to"
    pincode: str


CheckoutConstraint = Annotated[
    AllowedMerchantsConstraint | CheckoutAmountCeilingConstraint | CheckoutShipToConstraint,
    Field(discriminator="type"),
]


PaymentConstraint = Annotated[
    BudgetConstraint
    | AmountRangeConstraint
    | AllowedPayeesConstraint
    | ExecutionDateConstraint
    | ReferenceConstraint,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------


class CartItem(_Frozen):
    """One line of a cart, priced at the moment the merchant signed it."""

    sku: str
    name: str
    qty: int = Field(..., gt=0)
    unit_price: int = Field(..., ge=0, description="Paise.")
    line_total: int = Field(..., ge=0, description="Paise; qty * unit_price.")

    @model_validator(mode="after")
    def _line_total_consistent(self) -> CartItem:
        if self.line_total != self.qty * self.unit_price:
            raise ValueError(
                f"line_total {self.line_total} != qty {self.qty} * unit_price {self.unit_price}"
            )
        return self


class Cart(_Frozen):
    """A single-merchant cart.

    Single-merchant on purpose: splitting one Payment Mandate across payees would
    make ``payment.allowed_payees`` ambiguous, and multi-merchant routing is
    explicitly out of scope (see LIMITATIONS.md).
    """

    cart_id: str
    merchant_id: str
    merchant_name: str
    items: list[CartItem] = Field(..., min_length=1)
    currency: str = CURRENCY_INR
    total: int = Field(..., ge=0, description="Paise.")
    ship_to_pincode: str

    @model_validator(mode="after")
    def _total_consistent(self) -> Cart:
        expected = sum(i.line_total for i in self.items)
        if self.total != expected:
            raise ValueError(f"cart total {self.total} != sum of line totals {expected}")
        return self


# ---------------------------------------------------------------------------
# Checkout Mandate
# ---------------------------------------------------------------------------


class CheckoutMandateContents(_Frozen):
    """A Checkout Mandate, open or closed.

    **Open** (``mandate.checkout.open.1``) is signed by the user on a Trusted
    Surface and says, in effect: "you may check out at these merchants, up to
    this much per checkout, shipping to this pincode, until this time." It has no
    cart.

    **Closed** (``mandate.checkout.1``) is signed by the merchant and is a
    guarantee of one specific cart at one specific price for a short window. It
    has a cart, and it names the open mandate it was assembled under in
    ``delegate_chain``.

    The invariants below are enforced at construction, so a mandate that claims
    to be closed but carries no cart cannot exist as a Python object, let alone
    reach the verifier.
    """

    vct: CheckoutVct
    mandate_id: str
    # --- closed only -------------------------------------------------------
    cart: Cart | None = None
    # --- open only ---------------------------------------------------------
    #: Typed constraints, as docs/ap2/checkout_mandate.md specifies. An earlier
    #: version carried ad-hoc `allowed_merchants` / `max_amount` /
    #: `ship_to_pincode` fields; those are now `checkout.allowed_merchants` and
    #: two documented extensions, so the shape matches the spec.
    constraints: list[CheckoutConstraint] | None = None
    # --- both --------------------------------------------------------------
    currency: str = CURRENCY_INR
    delegate_chain: list[str] = Field(
        default_factory=list,
        description=(
            "Hashes of the mandates this one was derived from, oldest first. A "
            "closed Checkout Mandate lists the hash of the open Checkout Mandate "
            "it was assembled under. This is our plain-JWS analogue of the spec's "
            "`delegate_payload`, whose {'...': digest} shape only has meaning "
            "inside an SD-JWT; the binding it expresses is identical. See "
            "LIMITATIONS.md."
        ),
    )
    cnf: dict[str, Any] | None = Field(
        None, description="Key-binding confirmation claim (JWK), per RFC 7800."
    )

    @model_validator(mode="after")
    def _shape_matches_vct(self) -> CheckoutMandateContents:
        if self.vct == "mandate.checkout.1":
            if self.cart is None:
                raise ValueError("a closed Checkout Mandate must carry a cart")
        else:
            if self.cart is not None:
                raise ValueError("an open Checkout Mandate must not carry a cart")
            if not self.constraints:
                raise ValueError("an open Checkout Mandate must carry at least one constraint")
            if self.constraint("checkout.allowed_merchants") is None:
                raise ValueError(
                    "an open Checkout Mandate must carry a checkout.allowed_merchants "
                    "constraint; without it the authorisation covers every shop"
                )
        return self

    @property
    def is_open(self) -> bool:
        return self.vct == "mandate.checkout.open.1"

    def constraint(self, type_: str) -> CheckoutConstraint | None:
        """Return the single constraint of ``type_``, or ``None``.

        Raises on duplicates: two constraints of one type is ambiguous authority,
        and silently picking one would be a way to smuggle a looser bound past the
        merchant.
        """
        found = [c for c in (self.constraints or []) if c.type == type_]
        if len(found) > 1:
            raise ValueError(f"duplicate {type_} constraints in mandate {self.mandate_id}")
        return found[0] if found else None

    @property
    def allowed_merchant_ids(self) -> list[str]:
        constraint = self.constraint("checkout.allowed_merchants")
        return constraint.ids if isinstance(constraint, AllowedMerchantsConstraint) else []

    @property
    def max_amount(self) -> int | None:
        constraint = self.constraint("x-checkout.amount_ceiling")
        return constraint.max if isinstance(constraint, CheckoutAmountCeilingConstraint) else None

    @property
    def ship_to_pincode(self) -> str | None:
        constraint = self.constraint("x-checkout.ship_to")
        return constraint.pincode if isinstance(constraint, CheckoutShipToConstraint) else None


# ---------------------------------------------------------------------------
# Payment Mandate
# ---------------------------------------------------------------------------


class PaymentMandateContents(_Frozen):
    """A Payment Mandate, open or closed.

    **Open** (``mandate.payment.open.1``) is the user's standing authorisation:
    a list of :data:`PaymentConstraint` and nothing else. It never names an
    amount or a merchant directly — the constraints do that.

    **Closed** (``mandate.payment.1``) is one transaction: this much, to this
    payee, for this checkout, on this instrument, right now. It is signed by the
    shopping agent (or by the user directly, when a Trusted Surface escalation is
    approved) and it *embeds* the open mandate it claims authority from, in
    ``open_mandate_jws``. That embedding is what lets the verifier evaluate the
    user's constraints without trusting the agent to report them honestly.

    ``nonce`` is per-presentation and is burned on first use, which is what makes
    replay detectable.
    """

    vct: PaymentVct
    mandate_id: str
    nonce: str = Field(..., min_length=8, description="Single-use, per presentation.")
    # --- open only ---------------------------------------------------------
    constraints: list[PaymentConstraint] | None = None
    cnf: dict[str, Any] | None = None
    # --- closed only -------------------------------------------------------
    transaction_id: str | None = None
    payee: str | None = Field(None, description="Merchant id being paid.")
    payee_name: str | None = None
    payment_amount: int | None = Field(None, ge=0, description="Paise.")
    payment_instrument: str | None = Field(
        None, description="upi | card | payment_link — the rail-level method."
    )
    checkout_hash: str | None = Field(
        None, description="sha-256 hex of the merchant's closed Checkout Mandate JWS."
    )
    execution_date: datetime | None = None
    open_mandate_jws: str | None = Field(
        None,
        description=(
            "The user-signed OPEN Payment Mandate this closed mandate draws "
            "authority from, as a compact JWS. Single-hop: we do not support an "
            "arbitrary delegate chain."
        ),
    )
    # --- both --------------------------------------------------------------
    currency: str = CURRENCY_INR

    @model_validator(mode="after")
    def _shape_matches_vct(self) -> PaymentMandateContents:
        if self.vct == "mandate.payment.1":
            missing = [
                name
                for name in (
                    "transaction_id",
                    "payee",
                    "payment_amount",
                    "payment_instrument",
                    "checkout_hash",
                    "execution_date",
                    "open_mandate_jws",
                )
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    "a closed Payment Mandate is missing required fields: " + ", ".join(missing)
                )
            if self.constraints is not None:
                raise ValueError("a closed Payment Mandate carries no constraints of its own")
        else:
            if not self.constraints:
                raise ValueError("an open Payment Mandate must carry at least one constraint")
            if self.payment_amount is not None:
                raise ValueError("an open Payment Mandate must not name an amount")
        return self

    @property
    def is_open(self) -> bool:
        return self.vct == "mandate.payment.open.1"

    def constraint(self, type_: str) -> PaymentConstraint | None:
        """Return the single constraint of ``type_``, or ``None``.

        Raises if the mandate carries two constraints of the same type: that is
        ambiguous authority, and silently picking one would be a way to smuggle a
        looser bound past the verifier.
        """
        found = [c for c in (self.constraints or []) if c.type == type_]
        if len(found) > 1:
            raise ValueError(f"duplicate {type_} constraints in mandate {self.mandate_id}")
        return found[0] if found else None


# ---------------------------------------------------------------------------
# Receipts
#
# Receipts are not part of the AP2 mandate model — the spec describes them but
# does not fix a schema. Ours are signed JWS with a namespaced vct so they can
# never be confused with a mandate, and they double as the externally verifiable
# view of the audit chain: anyone holding the merchant/MPP public key can check a
# receipt without access to our database.
# ---------------------------------------------------------------------------


class CheckoutReceiptContents(_Frozen):
    """Issued by the Merchant when a Checkout Mandate is accepted."""

    vct: Literal["receipt.checkout.razorpay.1"] = VCT_CHECKOUT_RECEIPT
    receipt_id: str
    checkout_id: str
    status: Literal["confirmed"] = "confirmed"
    merchant_id: str
    merchant_name: str
    cart_id: str
    amount: int = Field(..., ge=0, description="Paise.")
    currency: str = CURRENCY_INR
    checkout_hash: str = Field(..., description="sha-256 hex of the closed Checkout Mandate JWS.")
    open_checkout_mandate_hash: str = Field(
        ..., description="sha-256 hex of the user's open Checkout Mandate JWS."
    )
    ts: datetime


class PaymentReceiptContents(_Frozen):
    """Issued by the Merchant Payment Processor for every terminal outcome.

    A failure gets a receipt too. An agent that asked for money and got silence
    cannot tell "declined" from "lost in transit", and that ambiguity is how
    double charges happen. ``status="failed"`` with a ``failure_code`` is a
    contract; a timeout is not.
    """

    vct: Literal["receipt.payment.razorpay.1"] = VCT_PAYMENT_RECEIPT
    receipt_id: str
    status: Literal["captured", "failed"]
    payment_mandate_id: str
    idempotency_key: str
    amount: int = Field(..., ge=0, description="Paise.")
    currency: str = CURRENCY_INR
    payee: str
    order_id: str | None = None
    payment_id: str | None = None
    method: str | None = None
    checkout_hash: str
    attempts: int = Field(1, ge=1, description="Payment attempts made under this mandate.")
    failure_code: str | None = None
    failure_reason: str | None = None
    ts: datetime

    @model_validator(mode="after")
    def _failure_is_explained(self) -> PaymentReceiptContents:
        if self.status == "failed" and not self.failure_code:
            raise ValueError("a failed PaymentReceipt must carry a failure_code")
        if self.status == "captured" and not self.payment_id:
            raise ValueError("a captured PaymentReceipt must carry a payment_id")
        return self
