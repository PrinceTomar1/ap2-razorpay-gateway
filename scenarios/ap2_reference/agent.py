"""An AP2 shopping agent written as an outsider would write one.

Google's reference Shopping Agent is built on A2A and google-adk, which this
project deliberately does not use (LIMITATIONS.md). Vendoring it would mean
pulling in a transport stack to prove a point about a mandate format. So this is
the honest equivalent: an agent implemented **only** from the published spec —
docs/ap2/specification.md, payment_mandate.md, checkout_mandate.md — with no
import from `ap2_min`, `gateway`, `merchant` or `shopping_agent`.

Why that constraint is the whole point. This agent:

* builds its mandate dicts **by hand**, from the spec's field names, so if our
  models drifted from the spec the JSON would not validate on arrival;
* signs with **plain PyJWT**, not our `Signer`, so a bug in our signing helper
  cannot cancel out a matching bug in our verification;
* speaks **MCP over a real client**, so it crosses the same serialisation
  boundary any third-party agent would;
* knows nothing about `unresolved_constraint` beyond what the spec says, and has
  to discover the human gate by being told about it.

If this agent can buy something, then the gateway genuinely implements AP2 for
somebody who has never read our source. That is a different and much stronger
claim than our own agent working.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

# --- The spec's vct strings, typed out by hand from the published document ----
VCT_CHECKOUT_OPEN = "mandate.checkout.open.1"
VCT_CHECKOUT_CLOSED = "mandate.checkout.1"
VCT_PAYMENT_OPEN = "mandate.payment.open.1"
VCT_PAYMENT_CLOSED = "mandate.payment.1"

ALGORITHM = "ES256"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@dataclass
class Transcript:
    """Everything the agent did, for the report."""

    steps: list[dict[str, Any]] = field(default_factory=list)

    def record(self, step: str, detail: str, payload: Any = None) -> None:
        self.steps.append({"step": step, "detail": detail, "payload": payload})


class ReferenceAgent:
    """An AP2 shopping agent with its own key, its own JWT code, and the spec."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey, kid: str) -> None:
        self._key = private_key
        self.kid = kid
        self.transcript = Transcript()

    # -- signing, done with plain PyJWT ------------------------------------

    def _sign(self, claims: dict[str, Any], *, ttl_seconds: int) -> str:
        """Compact JWS, ES256. No helper from the project under test."""
        from cryptography.hazmat.primitives import serialization

        pem = self._key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        issued = _now()
        envelope = {
            **claims,
            "iss": self.kid,
            "iat": int(issued.timestamp()),
            "exp": int((issued + timedelta(seconds=ttl_seconds)).timestamp()),
            "jti": claims.get("mandate_id") or uuid.uuid4().hex,
        }
        return str(jwt.encode(envelope, pem, algorithm=ALGORITHM, headers={"kid": self.kid}))

    # -- the mandate the spec describes, built by hand ---------------------

    def closed_payment_mandate(
        self,
        *,
        payee: str,
        payee_name: str,
        amount: int,
        checkout_jws: str,
        open_mandate_jws: str,
        instrument: str = "upi",
    ) -> str:
        """A `mandate.payment.1`, field by field from payment_mandate.md.

        `payment.reference.conditional_transaction_id` is sha-256 of the merchant's
        signed Checkout Mandate — the spec says the algorithm is the SD-JWT's
        `_sd_alg`, or sha-256 when undefined. This is not an SD-JWT, so sha-256.
        """
        checkout_hash = hashlib.sha256(checkout_jws.encode()).hexdigest()
        now = _now()
        claims: dict[str, Any] = {
            "vct": VCT_PAYMENT_CLOSED,
            "mandate_id": f"pm_{uuid.uuid4().hex[:16]}",
            "nonce": uuid.uuid4().hex,
            "transaction_id": f"txn_{uuid.uuid4().hex[:16]}",
            "payee": payee,
            "payee_name": payee_name,
            "payment_amount": amount,
            "currency": "INR",
            "payment_instrument": instrument,
            "checkout_hash": checkout_hash,
            "execution_date": _iso(now),
            "open_mandate_jws": open_mandate_jws,
        }
        self.transcript.record(
            "sign closed Payment Mandate",
            f"vct={VCT_PAYMENT_CLOSED} amount={amount} payee={payee} "
            f"checkout_hash={checkout_hash[:16]}…",
            {k: v for k, v in claims.items() if k != "open_mandate_jws"},
        )
        return self._sign(claims, ttl_seconds=600)

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._key.public_key()
