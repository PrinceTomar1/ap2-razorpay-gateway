"""Cryptographic envelope for AP2 mandates and receipts: ES256 compact JWS.

# AP2 uses SD-JWT for selective disclosure; we use plain JWS.
#
# An SD-JWT lets the holder reveal *some* claims of a mandate to one party and
# withhold the rest — a shopping agent could prove to a processor that a payment
# is within budget without revealing the budget. We sign the whole mandate as one
# ES256 compact JWS instead. Every verifier in this system therefore sees every
# claim. That is a real reduction in privacy and it is recorded in LIMITATIONS.md;
# it is not a reduction in *integrity*, which is the property the money path
# depends on. Swapping the envelope for SD-JWT is a change to this module and
# gateway/verify.py's signature checks, and nothing else.

Layering: this module knows about keys, bytes and signatures. It knows nothing
about budgets, carts or Razorpay. Policy lives in gateway/verify.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeVar

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import BaseModel, ValidationError

from ap2_min.models import CheckoutMandateContents, PaymentMandateContents

ALGORITHM: Final = "ES256"

#: Claims the JWS envelope owns. They are added at sign time and stripped before
#: a payload is parsed back into a content model (which forbids extra fields).
RESERVED_CLAIMS: Final = frozenset({"iat", "exp", "nbf", "jti", "iss", "aud", "sub"})

M = TypeVar("M", bound=BaseModel)


# ---------------------------------------------------------------------------
# Typed errors
#
# Failure mode 3 ("invalid mandate") requires rejection at the boundary with a
# typed error and nothing reaching Razorpay. These are that boundary. Every one
# carries a stable machine `code` so the agent can branch on it, and a
# human-readable message that is safe to surface.
# ---------------------------------------------------------------------------


class MandateError(Exception):
    """Base class for every mandate-envelope failure."""

    code = "mandate.invalid"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "detail": self.detail}


class MandateMalformedError(MandateError):
    """Not a compact JWS, or the payload does not fit the declared model."""

    code = "mandate.malformed"


class MandateSignatureError(MandateError):
    """The signature does not verify under the key named by ``kid``."""

    code = "mandate.bad_signature"


class MandateExpiredError(MandateError):
    """``exp`` is in the past (or ``nbf`` is in the future)."""

    code = "mandate.expired"


class UnknownKeyError(MandateError):
    """``kid`` names a key this gateway does not trust."""

    code = "mandate.unknown_key"


class UntrustedIssuerError(MandateError):
    """The key is known, but its role is not the one this call requires.

    A merchant key signing something that must come from the user is not a
    signature failure — it verifies fine. It is an *authority* failure, and it is
    the more interesting of the two.
    """

    code = "mandate.wrong_issuer"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Timezone-aware UTC now, truncated to whole seconds.

    Whole seconds because JWT ``exp``/``iat`` are integer seconds; keeping
    microseconds around only creates off-by-a-microsecond comparisons between the
    model field and the envelope claim.
    """
    return datetime.now(UTC).replace(microsecond=0)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def jws_hash(token: str) -> str:
    """sha-256 hex of a compact JWS, as bytes on the wire.

    Hashing the *token* rather than the decoded claims is deliberate: it is
    unambiguous (no canonicalisation question) and it binds to one specific
    signature, so a re-signed mandate with identical contents is a different
    hash. That is what we want for ``payment.reference``.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def checkout_hash(checkout_jwt: str) -> str:
    """sha-256 hex of a Checkout Mandate JWS.

    This is the value a closed Payment Mandate must carry in ``checkout_hash``
    and that a ``payment.reference`` constraint carries in
    ``conditional_transaction_id``. The spec permits ``_sd_alg`` to select the
    algorithm; we are not an SD-JWT, so it is sha-256.
    """
    return jws_hash(checkout_jwt)


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace.

    Used by the audit chain so a hash computed today matches one computed by a
    reviewer next month on a different machine.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def new_id(prefix: str) -> str:
    """A short, readable, collision-free identifier: ``pm_3f9c1a...``."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def new_nonce() -> str:
    """A single-use nonce for one mandate presentation."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[ec.EllipticCurvePrivateKey, str]:
    """Generate a P-256 keypair. Returns the private key and its public PEM.

    P-256 because AP2 fixes ES256 (docs/ap2/payment_mandate.md: "Algorithm:
    ES256"), and ES256 means ECDSA over P-256 with SHA-256.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_key, public_pem


def public_jwk(private_key: ec.EllipticCurvePrivateKey) -> dict[str, Any]:
    """The public half as a JWK, for the ``cnf`` key-binding claim (RFC 7800).

    AP2 open mandates carry ``cnf`` so a verifier can tell which key is entitled
    to present the closed mandate derived from it.
    """
    numbers = private_key.public_key().public_numbers()
    size = 32  # P-256 coordinates are 32 bytes

    def b64u(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode("ascii")

    return {"kty": "EC", "crv": "P-256", "x": b64u(numbers.x), "y": b64u(numbers.y)}


@dataclass(frozen=True)
class Signer:
    """A private key plus the identity it signs as.

    ``role`` is the interesting field. Verification asks "was this signed by the
    *user*?", not "was this signed by key abc123?", and the keyring answers that
    by role. See :class:`KeyRing`.
    """

    kid: str
    role: str
    private_key: ec.EllipticCurvePrivateKey

    @property
    def public_pem(self) -> str:
        return (
            self.private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )

    @property
    def cnf(self) -> dict[str, Any]:
        """A ``cnf`` claim binding to this signer's public key."""
        return {"jwk": public_jwk(self.private_key)}

    def sign(
        self,
        contents: BaseModel,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
        jti: str | None = None,
        audience: str | None = None,
    ) -> str:
        """Serialise ``contents`` and sign it as an ES256 compact JWS."""
        issued = now or utcnow()
        payload: dict[str, Any] = dict(contents.model_dump(mode="json"))
        overlap = RESERVED_CLAIMS & payload.keys()
        if overlap:
            # A content model that defines its own `exp` would let a caller set an
            # expiry the envelope disagrees with. Fail loudly at build time.
            raise ValueError(
                f"content model must not define reserved JWS claims: {sorted(overlap)}"
            )
        payload.update(
            {
                "iss": self.kid,
                "iat": int(issued.timestamp()),
                "exp": int((issued + timedelta(seconds=ttl_seconds)).timestamp()),
                "jti": jti or getattr(contents, "mandate_id", None) or new_id("jws"),
            }
        )
        if audience is not None:
            payload["aud"] = audience
        return jwt.encode(
            payload,
            self.private_key,
            algorithm=ALGORITHM,
            headers={"kid": self.kid, "typ": "JWT"},
        )


@dataclass(frozen=True)
class TrustedKey:
    kid: str
    role: str
    public_pem: str


class KeyRing:
    """The set of public keys this gateway trusts, indexed by ``kid``.

    In production this is a key-distribution problem (JWKS endpoints, a registry,
    rotation). Here every party registers at startup, which keeps the demo
    offline. What matters for the security argument is unchanged: a mandate
    signed by a key that is not in the ring is rejected before any check runs,
    and a mandate signed by the *wrong role* is rejected even though its
    signature is perfectly valid.
    """

    def __init__(self) -> None:
        self._keys: dict[str, TrustedKey] = {}

    def register(self, kid: str, role: str, public_pem: str) -> None:
        existing = self._keys.get(kid)
        if existing is not None and existing.public_pem != public_pem:
            raise ValueError(f"refusing to silently rotate key {kid!r}")
        self._keys[kid] = TrustedKey(kid=kid, role=role, public_pem=public_pem)

    def register_signer(self, signer: Signer) -> None:
        self.register(signer.kid, signer.role, signer.public_pem)

    def get(self, kid: str) -> TrustedKey:
        try:
            return self._keys[kid]
        except KeyError:
            raise UnknownKeyError(
                f"no trusted key registered for kid {kid!r}", detail="unknown_kid"
            ) from None

    def kids(self) -> list[str]:
        return sorted(self._keys)


# ---------------------------------------------------------------------------
# Encode / decode
# ---------------------------------------------------------------------------


def decode_unverified(token: str) -> dict[str, Any]:
    """Decode a JWS *without* checking its signature.

    Only ever for logging, routing and error messages. Never call this on the
    money path — nothing it returns is trustworthy. It exists because when a
    mandate is rejected we still want the audit row to record what was claimed.
    """
    try:
        return dict(jwt.decode(token, options={"verify_signature": False}))
    except jwt.PyJWTError as exc:
        raise MandateMalformedError("not a decodable JWS", detail=str(exc)) from exc


def unverified_kid(token: str) -> str | None:
    """Read ``kid`` from the JWS header without verifying anything."""
    try:
        return jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None


def verify_jws(
    token: str,
    keyring: KeyRing,
    *,
    expected_role: str | None = None,
    leeway_seconds: int = 30,
    audience: str | None = None,
) -> dict[str, Any]:
    """Verify a compact JWS and return its claims.

    Raises a typed :class:`MandateError` — never a bare ``PyJWTError`` — so every
    caller on the money path can map the failure to a stable code without
    inspecting exception text.

    ``expected_role`` is the authority check: "this must have been signed by the
    user" is a different question from "this signature is valid", and both must
    hold.
    """
    if not isinstance(token, str) or token.count(".") != 2:
        raise MandateMalformedError("expected a compact JWS with three segments")

    kid = unverified_kid(token)
    if not kid:
        raise MandateMalformedError("JWS header carries no kid", detail="missing_kid")
    trusted = keyring.get(kid)

    if expected_role is not None and trusted.role != expected_role:
        raise UntrustedIssuerError(
            f"mandate was signed by role {trusted.role!r}, this call requires {expected_role!r}",
            detail=f"expected_role={expected_role} actual_role={trusted.role}",
        )

    options: dict[str, Any] = {"require": ["exp", "iat", "iss", "jti"]}
    if audience is None:
        options["verify_aud"] = False
    try:
        claims = jwt.decode(
            token,
            trusted.public_pem,
            algorithms=[ALGORITHM],
            leeway=leeway_seconds,
            options=options,
            audience=audience,
        )
    except jwt.ExpiredSignatureError as exc:
        raise MandateExpiredError("mandate has expired", detail=str(exc)) from exc
    except jwt.ImmatureSignatureError as exc:
        raise MandateExpiredError("mandate is not yet valid", detail=str(exc)) from exc
    except jwt.InvalidSignatureError as exc:
        raise MandateSignatureError("signature does not verify", detail=str(exc)) from exc
    except jwt.MissingRequiredClaimError as exc:
        raise MandateMalformedError("mandate is missing a required claim", detail=str(exc)) from exc
    except jwt.InvalidAlgorithmError as exc:
        # Refusing `alg: none` and RSA-for-EC confusion is not optional.
        raise MandateSignatureError("unsupported signing algorithm", detail=str(exc)) from exc
    except jwt.PyJWTError as exc:
        raise MandateMalformedError("mandate could not be decoded", detail=str(exc)) from exc

    if claims.get("iss") != kid:
        raise MandateMalformedError(
            "iss does not match the header kid", detail=f"iss={claims.get('iss')} kid={kid}"
        )
    return dict(claims)


def load_contents(claims: Mapping[str, Any], model: type[M]) -> M:
    """Parse verified claims into a content model, stripping envelope claims.

    The content models set ``extra="forbid"``, so this is also where a mandate
    carrying an unexpected field is rejected — quietly dropping unknown keys is
    how a "harmless" extra claim turns into a parser-differential bug.
    """
    body = {k: v for k, v in claims.items() if k not in RESERVED_CLAIMS}
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise MandateMalformedError(
            f"payload does not satisfy {model.__name__}",
            detail="; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            ),
        ) from exc


def verify_and_load(
    token: str,
    keyring: KeyRing,
    model: type[M],
    *,
    expected_role: str | None = None,
    leeway_seconds: int = 30,
) -> tuple[M, dict[str, Any]]:
    """Verify a JWS and parse it. Returns ``(contents, raw_claims)``.

    Callers that need ``exp``/``iat`` (the verifier does) read them from
    ``raw_claims``; callers that need the mandate read ``contents``.
    """
    claims = verify_jws(token, keyring, expected_role=expected_role, leeway_seconds=leeway_seconds)
    return load_contents(claims, model), claims


def load_checkout_mandate(
    token: str, keyring: KeyRing, *, expected_role: str | None = None
) -> tuple[CheckoutMandateContents, dict[str, Any]]:
    return verify_and_load(token, keyring, CheckoutMandateContents, expected_role=expected_role)


def load_payment_mandate(
    token: str, keyring: KeyRing, *, expected_role: str | None = None
) -> tuple[PaymentMandateContents, dict[str, Any]]:
    return verify_and_load(token, keyring, PaymentMandateContents, expected_role=expected_role)
