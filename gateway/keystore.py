"""Signing keys that survive a restart, when you ask for them.

A receipt claims a year of validity — `test_receipts_are_long_lived` asserts it —
and a dispute is months later. But keys were generated fresh in memory on every
boot, so a receipt issued before a restart could not be verified after one. The
signature was sound; the public half that would prove it no longer existed.

That gap was found by probing rather than by reading, and it mattered: the whole
evidential claim of this project is that a receipt is checkable by a third party
long after the fact.

**The default is still ephemeral.** Tests and `make demo` generate throwaway keys
and should — an in-memory run has nothing to be evidence for, and writing key
material during a test run would be worse than useless. A keystore is opt-in via
``GATEWAY_KEYSTORE``, and `make serve` sets it, because a gateway that forgets who
it is on restart cannot honour the receipts it has already issued.

**This is not a KMS.** Private keys sit in a file with 0600 permissions, which is
the right shape for a single-process gateway holding test-mode credentials and
the wrong shape for production. LIMITATIONS.md says so. The seam is here so that
swapping in a KMS means replacing this module and nothing else.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

#: Only the owner may read a file holding private keys.
_OWNER_ONLY = stat.S_IRUSR | stat.S_IWUSR


class KeystoreError(RuntimeError):
    """The keystore exists but cannot be used. Never guessed around."""


def _serialise(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _deserialise(pem: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise KeystoreError("keystore holds a key that is not an EC private key")
    if key.curve.name != "secp256r1":
        raise KeystoreError(f"keystore holds a {key.curve.name} key; ES256 needs secp256r1")
    return key


class Keystore:
    """Load-or-create P-256 private keys, keyed by role name.

    Deliberately dumb: a JSON file of PEMs. It does not rotate, revoke, or
    expire anything — see LIMITATIONS.md. What it does do is make a receipt
    verifiable tomorrow, which is the property that was silently missing.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._keys: dict[str, ec.EllipticCurvePrivateKey] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KeystoreError(
                f"{self.path} is not a readable keystore ({exc}). Delete it to start "
                "fresh — but every receipt signed with the old keys becomes "
                "unverifiable, so move it aside rather than removing it."
            ) from exc
        if not isinstance(raw, dict):
            raise KeystoreError(f"{self.path} is not a keystore object")
        self._keys = {str(name): _deserialise(str(pem)) for name, pem in raw.items()}

    def _persist(self) -> None:
        body = json.dumps({name: _serialise(key) for name, key in self._keys.items()}, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with owner-only permissions from the outset — writing world
        # readable and chmod-ing after leaves a window where the key is exposed.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_ONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
        os.chmod(self.path, _OWNER_ONLY)

    def key_for(self, name: str) -> ec.EllipticCurvePrivateKey:
        """The key for ``name``, generating and persisting one on first use."""
        existing = self._keys.get(name)
        if existing is not None:
            return existing
        created = ec.generate_private_key(ec.SECP256R1())
        self._keys[name] = created
        self._persist()
        return created

    @property
    def names(self) -> list[str]:
        return sorted(self._keys)

    def insecure_permissions(self) -> bool:
        """True if anyone but the owner can read the file. Worth refusing to ignore."""
        if not self.path.is_file():
            return False
        return bool(self.path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
