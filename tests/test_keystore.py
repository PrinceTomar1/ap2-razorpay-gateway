"""Signing keys that survive a restart.

The bug this file exists for: `test_receipts_are_long_lived` asserts a receipt is
valid for over 300 days, and a dispute is months later — but keys were generated
fresh on every boot, so a receipt issued before a restart could not be verified
after one. The signature was sound; the public half that would prove it was gone.

Found by probing, not by reading. Nothing in the suite exercised a restart.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from ap2_min.builders import closed_payment_mandate
from ap2_min.models import PaymentReceiptContents
from ap2_min.roles import ROLE_MPP
from gateway.bootstrap import Gateway, build_gateway
from gateway.config import ConfigurationError
from gateway.keystore import Keystore, KeystoreError
from gateway.mandates import utcnow, verify_and_load


def _buy(gateway: Gateway) -> dict[str, Any]:
    merchant = gateway.merchant
    cart = merchant.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    merchant.complete_checkout(checkout["checkout_id"], gateway.open_checkout_jws)
    now = utcnow()
    contents = closed_payment_mandate(
        payee=cart["merchant_id"],
        payee_name=cart["merchant_name"],
        amount=cart["total"],
        payment_instrument="upi",
        checkout_hash=checkout["checkout_hash"],
        open_mandate_jws=gateway.open_payment_jws,
        execution_date=now,
    )
    signed = gateway.agent.sign(contents, ttl_seconds=600, now=now)
    return dict(merchant.initiate_payment(checkout["checkout_id"], signed))


# ---------------------------------------------------------------------------
# The property that was missing
# ---------------------------------------------------------------------------


def test_a_receipt_still_verifies_after_a_restart(tmp_path: Path) -> None:
    """The whole point. A receipt is evidence or it is decoration."""
    keystore = tmp_path / "keystore.json"
    database = tmp_path / "gateway.db"

    first = build_gateway(db_path=database, keystore=keystore, use_llm=False, sleep=lambda _s: None)
    try:
        receipt_jws = _buy(first)["payment_receipt_jws"]
    finally:
        first.close()

    second = build_gateway(
        db_path=database, keystore=keystore, use_llm=False, sleep=lambda _s: None
    )
    try:
        receipt, _ = verify_and_load(
            receipt_jws, second.keyring, PaymentReceiptContents, expected_role=ROLE_MPP
        )
        assert receipt.status == "captured"
    finally:
        second.close()


def test_without_a_keystore_a_receipt_does_not_survive_a_restart(tmp_path: Path) -> None:
    """The old behaviour, asserted so nobody mistakes it for an accident.

    Ephemeral keys remain the default and this is the cost of that default. It is
    the right default for a test run or an offline demo — neither has anything to
    be evidence for — and the wrong one for `make serve`, which is why that sets
    a keystore.
    """
    from gateway.mandates import MandateSignatureError

    database = tmp_path / "gateway.db"
    first = build_gateway(db_path=database, use_llm=False, sleep=lambda _s: None)
    try:
        receipt_jws = _buy(first)["payment_receipt_jws"]
    finally:
        first.close()

    second = build_gateway(db_path=database, use_llm=False, sleep=lambda _s: None)
    try:
        with pytest.raises(MandateSignatureError):
            verify_and_load(
                receipt_jws, second.keyring, PaymentReceiptContents, expected_role=ROLE_MPP
            )
    finally:
        second.close()


def test_the_same_keys_come_back(tmp_path: Path) -> None:
    keystore = tmp_path / "keystore.json"
    first = build_gateway(keystore=keystore, use_llm=False, sleep=lambda _s: None)
    second = build_gateway(keystore=keystore, use_llm=False, sleep=lambda _s: None)
    try:
        for signer in ("user", "agent", "merchant_signer", "mpp"):
            a = getattr(first, signer)
            b = getattr(second, signer)
            assert a.kid == b.kid
            assert first.keyring.get(a.kid).public_pem == second.keyring.get(b.kid).public_pem
    finally:
        first.close()
        second.close()


def test_ephemeral_keys_really_are_different_each_time() -> None:
    """The default must not silently become persistent."""
    first = build_gateway(use_llm=False, sleep=lambda _s: None)
    second = build_gateway(use_llm=False, sleep=lambda _s: None)
    try:
        assert first.keyring.get(first.user.kid).public_pem != (
            second.keyring.get(second.user.kid).public_pem
        )
    finally:
        first.close()
        second.close()


# ---------------------------------------------------------------------------
# The file itself
# ---------------------------------------------------------------------------


def test_the_keystore_is_created_owner_only(tmp_path: Path) -> None:
    """It holds private keys. Anything wider is a leak waiting to happen."""
    path = tmp_path / "keystore.json"
    Keystore(path).key_for("key_test")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_world_readable_keystore_refuses_to_start(tmp_path: Path) -> None:
    """Failing loudly beats signing with a key anybody can read."""
    path = tmp_path / "keystore.json"
    Keystore(path).key_for("key_user_buyer")
    os.chmod(path, 0o644)

    with pytest.raises(ConfigurationError, match="readable by somebody other than its owner"):
        build_gateway(keystore=path, use_llm=False, sleep=lambda _s: None)


def test_keys_are_generated_lazily_and_persisted_immediately(tmp_path: Path) -> None:
    path = tmp_path / "keystore.json"
    store = Keystore(path)
    assert store.names == []

    store.key_for("key_a")
    assert json.loads(path.read_text(encoding="utf-8")).keys() == {"key_a"}

    store.key_for("key_b")
    assert Keystore(path).names == ["key_a", "key_b"]


def test_the_same_name_returns_the_same_key(tmp_path: Path) -> None:
    store = Keystore(tmp_path / "keystore.json")
    assert store.key_for("key_a") is store.key_for("key_a")


def test_a_corrupt_keystore_is_refused_not_silently_replaced(tmp_path: Path) -> None:
    """Regenerating would make every issued receipt unverifiable, quietly."""
    path = tmp_path / "keystore.json"
    path.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(KeystoreError, match="not a readable keystore"):
        Keystore(path)


def test_a_keystore_holding_the_wrong_curve_is_refused(tmp_path: Path) -> None:
    """ES256 means P-256. A P-384 key would fail at signing time instead."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    wrong = ec.generate_private_key(ec.SECP384R1())
    pem = wrong.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "keystore.json"
    path.write_text(json.dumps({"key_user_buyer": pem}), encoding="utf-8")

    with pytest.raises(KeystoreError, match="secp256r1"):
        Keystore(path)


def test_a_keystore_holding_a_non_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "keystore.json"
    path.write_text(json.dumps({"key_user_buyer": "-----BEGIN NOT A KEY-----"}), encoding="utf-8")
    with pytest.raises(Exception, match=r"(?i)key|pem|load"):
        Keystore(path)


def test_a_missing_keystore_is_created_rather_than_failing(tmp_path: Path) -> None:
    """First run of `make serve` must just work."""
    path = tmp_path / "nested" / "dir" / "keystore.json"
    gateway = build_gateway(keystore=path, use_llm=False, sleep=lambda _s: None)
    try:
        assert path.is_file()
        assert Keystore(path).names == [
            "key_agent_shopper",
            "key_merchant_gateway",
            "key_mpp_razorpay",
            "key_user_buyer",
        ]
    finally:
        gateway.close()


def test_the_keystore_is_gitignored() -> None:
    """A private key in a public repository is the worst possible outcome."""
    ignored = Path(__file__).resolve().parent.parent / ".gitignore"
    assert "keystore.json" in ignored.read_text(encoding="utf-8")


def test_the_environment_variable_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "from_env.json"
    monkeypatch.setenv("GATEWAY_KEYSTORE", str(path))
    gateway = build_gateway(use_llm=False, sleep=lambda _s: None)
    try:
        assert path.is_file()
    finally:
        gateway.close()
