"""The AP2 roles, as named in docs/ap2/specification.md.

The specification defines five. This repository implements two of them
(Merchant, Merchant Payment Processor), simulates two (Shopping Agent, Trusted
Surface) so there is something to verify against, and stubs none — the
Credential Provider is out of scope and is recorded in LIMITATIONS.md.

These strings are the ``role`` field of a :class:`gateway.mandates.TrustedKey`.
They are how the verifier asks "was this signed by the *user*?" instead of the
much weaker "is this signature valid?".
"""

from typing import Final

#: Performs product discovery and purchase execution. Holds NO payment authority
#: of its own — it can only present mandates a user signed.
ROLE_SHOPPING_AGENT: Final = "shopping_agent"

#: Validates checkout integrity and shopping-agent authorisation. Implemented
#: here as merchant/mcp_server.py + merchant/checkout.py.
ROLE_MERCHANT: Final = "merchant"

#: Processes payments and verifies credential authorisation. Implemented here as
#: gateway/verify.py + gateway/payments.py + gateway/razorpay_client.py.
ROLE_MPP: Final = "merchant_payment_processor"

#: The human. The only role that can sign an OPEN mandate — a standing
#: authorisation is by definition something only the money's owner can grant.
ROLE_USER: Final = "user"

#: Non-agentic UI that obtains informed consent and produces user-signed
#: mandates. Implemented here as gateway/trusted_surface.py; it signs *as the
#: user*, which is why it must never contain a model.
ROLE_TRUSTED_SURFACE: Final = "trusted_surface"

#: Verifies agent authorisation for payment credentials. NOT implemented.
ROLE_CREDENTIAL_PROVIDER: Final = "credential_provider"
