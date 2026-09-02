"""ap2_min — a vendored, minimal subset of the AP2 v0.2 data model.

Why this exists instead of `pip install ap2`
--------------------------------------------
Google's reference AP2 implementation (github.com/google-agentic-commerce/AP2,
Apache-2.0) is not published to PyPI. The `ap2` distribution that *is* on PyPI is
an unaffiliated third-party mirror whose model surface is the A2A/ADK sample
shapes (``CartMandate``, ``IntentMandate``) rather than the v0.2 specification's
open/closed mandate model with ``vct`` claims and typed constraints — which is
exactly the part this project needs to be faithful about. It also drags in
google-adk, google-genai and a2a-sdk as extras we do not use.

So we vendor the handful of types we need, transcribed from the specification
documents:

  docs/ap2/specification.md
  docs/ap2/payment_mandate.md
  docs/ap2/checkout_mandate.md

Field names, ``vct`` strings and constraint ``type`` strings match the spec
exactly. See DECISIONS.md ("vendored ap2_min").

Deliberate divergences from the spec, all recorded in LIMITATIONS.md:

* **Plain compact JWS, not SD-JWT.** AP2 uses SD-JWT for selective disclosure.
  We sign the whole mandate as one ES256 compact JWS. Nothing here is selectively
  disclosable.
* **Integer paise, not floating point.** The W3C ``PaymentCurrencyAmount`` the
  spec borrows types ``value`` as a float. Float money is a correctness bug in a
  payments system, so every amount in this package is an ``int`` count of paise
  (₹1 = 100 paise), and INR is the only currency.
* **Single-hop delegation.** A closed mandate carries the one open mandate it
  derives from, not an arbitrary delegate chain.
"""

from ap2_min.models import (
    AllowedPayeesConstraint,
    AmountRangeConstraint,
    BudgetConstraint,
    Cart,
    CartItem,
    CheckoutMandateContents,
    CheckoutReceiptContents,
    ExecutionDateConstraint,
    PaymentConstraint,
    PaymentMandateContents,
    PaymentReceiptContents,
    ReferenceConstraint,
    inr,
    paise_to_inr_str,
)
from ap2_min.vct import (
    VCT_CHECKOUT_CLOSED,
    VCT_CHECKOUT_OPEN,
    VCT_PAYMENT_CLOSED,
    VCT_PAYMENT_OPEN,
    CheckoutVct,
    PaymentVct,
)

__all__ = [
    "VCT_CHECKOUT_CLOSED",
    "VCT_CHECKOUT_OPEN",
    "VCT_PAYMENT_CLOSED",
    "VCT_PAYMENT_OPEN",
    "AllowedPayeesConstraint",
    "AmountRangeConstraint",
    "BudgetConstraint",
    "Cart",
    "CartItem",
    "CheckoutMandateContents",
    "CheckoutReceiptContents",
    "CheckoutVct",
    "ExecutionDateConstraint",
    "PaymentConstraint",
    "PaymentMandateContents",
    "PaymentReceiptContents",
    "PaymentVct",
    "ReferenceConstraint",
    "inr",
    "paise_to_inr_str",
]
