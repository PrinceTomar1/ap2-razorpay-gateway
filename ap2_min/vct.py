"""AP2 verifiable-credential-type (``vct``) claims.

From docs/ap2/specification.md:

    "Implementations MUST match the exact ``vct`` string, including the version
    suffix."

That is why these are module-level constants used by both the signers and the
verifier, rather than string literals sprinkled through the code: an exact-match
check is only as good as its single source of truth.

The open/closed distinction is the heart of the protocol:

* **open**  — a standing authorisation. Carries *constraints*, not a transaction.
              Signed by the user on a Trusted Surface. Long-lived.
* **closed** — one specific transaction. Carries concrete amounts, payee and
              cart. Signed by whoever is presenting it (the shopping agent, or
              the user directly when a Trusted Surface escalation is approved).
              Short-lived.
"""

from typing import Final, Literal

VCT_CHECKOUT_OPEN: Final = "mandate.checkout.open.1"
VCT_CHECKOUT_CLOSED: Final = "mandate.checkout.1"
VCT_PAYMENT_OPEN: Final = "mandate.payment.open.1"
VCT_PAYMENT_CLOSED: Final = "mandate.payment.1"

CheckoutVct = Literal["mandate.checkout.open.1", "mandate.checkout.1"]
PaymentVct = Literal["mandate.payment.open.1", "mandate.payment.1"]

#: Receipt types. These are ours, not AP2's — the specification describes
#: receipts but does not fix a ``vct`` for them. Namespaced so they can never
#: collide with a future AP2 claim.
VCT_CHECKOUT_RECEIPT: Final = "receipt.checkout.razorpay.1"
VCT_PAYMENT_RECEIPT: Final = "receipt.payment.razorpay.1"
