"""Security review, as tests.

Each of these is a question a payments security reviewer would ask, answered by
an assertion rather than a paragraph. The threat model they correspond to is in
SECURITY.md.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ap2_min.models import inr
from gateway.app import create_app
from gateway.audit import AuditLog, Event
from gateway.bootstrap import Gateway
from gateway.db import Database
from gateway.razorpay_client import FakeRail
from gateway.webhooks import WebhookReceiver, verify_webhook_signature

from .test_failure_modes import confirmed_checkout, signed_payment

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET = "a_test_webhook_secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def receiver() -> WebhookReceiver:
    return WebhookReceiver(audit=AuditLog(Database()), secret=SECRET)


# ===========================================================================
# Signature bypass
# ===========================================================================


def test_all_jws_handling_is_funnelled_through_one_module() -> None:
    """One door means one place to review, and one place to get wrong.

    If `jwt.decode` appears anywhere else, this file's guarantees stop covering
    the whole system.
    """
    packages = ("ap2_min", "gateway", "merchant", "llm", "shopping_agent", "demo")
    offenders = []
    for package in packages:
        for path in (REPO_ROOT / package).rglob("*.py"):
            if path.name == "mandates.py":
                continue
            source = path.read_text(encoding="utf-8")
            if "jwt.decode" in source or "jwt.encode" in source:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"raw JWT handling outside gateway/mandates.py: {offenders}"


def test_nothing_on_the_money_path_acts_on_unverified_claims() -> None:
    """`decode_unverified` exists for logging. It must never reach a decision."""
    for module in ("gateway/verify.py", "gateway/payments.py", "gateway/recovery.py"):
        source = (REPO_ROOT / module).read_text(encoding="utf-8")
        assert "decode_unverified" not in source, f"{module} reads unverified claims"
        assert "verify_signature" not in source


def test_the_only_unverified_read_is_the_kid_lookup() -> None:
    """Reading `kid` before verifying is unavoidable — you need it to pick the key.

    What matters is that nothing else is read before verification, and that the
    key it selects then has to actually verify the signature.
    """
    source = (REPO_ROOT / "gateway" / "mandates.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "unverified_kid"
    ]
    assert len(calls) == 1, "kid is read unverified in exactly one place"


@pytest.mark.parametrize(
    "algorithm",
    ["none", "HS256", "RS256", "ES384", "EdDSA"],
)
def test_only_es256_is_accepted(wired: Gateway, fake_rail: FakeRail, algorithm: str) -> None:
    """A header naming any other algorithm must be refused, however it is signed."""
    checkout = confirmed_checkout(wired)

    def b64u(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = b64u(json.dumps({"alg": algorithm, "kid": wired.agent.kid}).encode())
    payload = b64u(
        json.dumps(
            {
                "vct": "mandate.payment.1",
                "iss": wired.agent.kid,
                "iat": 1,
                "exp": 99999999999,
                "jti": "x",
            }
        ).encode()
    )
    # Signed with the public key as an HMAC secret — the classic confusion attack.
    public_pem = wired.keyring.get(wired.agent.kid).public_pem
    forged = b64u(
        hmac.new(public_pem.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )

    response = wired.merchant.initiate_payment(
        checkout["checkout_id"], f"{header}.{payload}.{forged}"
    )
    assert response["error"].startswith("mandate."), f"{algorithm} was not refused"
    assert fake_rail.calls == []


def test_an_empty_signature_segment_is_refused(wired: Gateway, fake_rail: FakeRail) -> None:
    checkout = confirmed_checkout(wired)
    token = signed_payment(wired, checkout)
    header, payload, _ = token.split(".")
    response = wired.merchant.initiate_payment(checkout["checkout_id"], f"{header}.{payload}.")
    assert response["error"].startswith("mandate.")
    assert fake_rail.calls == []


def test_a_receipt_cannot_be_forged_by_the_merchant(wired: Gateway) -> None:
    """Only the processor's key attests that money moved."""
    from ap2_min.models import PaymentReceiptContents
    from ap2_min.roles import ROLE_MPP
    from gateway.mandates import UntrustedIssuerError, verify_and_load

    checkout = confirmed_checkout(wired)
    paid = wired.merchant.initiate_payment(checkout["checkout_id"], signed_payment(wired, checkout))
    receipt = PaymentReceiptContents.model_validate(paid["payment_receipt"])
    forged = wired.merchant_signer.sign(receipt, ttl_seconds=600)

    with pytest.raises(UntrustedIssuerError):
        verify_and_load(forged, wired.keyring, PaymentReceiptContents, expected_role=ROLE_MPP)


# ===========================================================================
# Webhook signature and replay
# ===========================================================================


def test_a_valid_webhook_signature_is_accepted(receiver: WebhookReceiver) -> None:
    body = json.dumps({"event": "payment.captured"}).encode()
    result = receiver.handle(body, _sign(body), "evt_001")
    assert result["received"] is True
    assert result["duplicate"] is False


def test_a_forged_webhook_signature_is_refused_and_audited(
    receiver: WebhookReceiver,
) -> None:
    from fastapi import HTTPException

    body = json.dumps({"event": "payment.captured"}).encode()
    with pytest.raises(HTTPException) as excinfo:
        receiver.handle(body, "deadbeef", "evt_002")
    assert excinfo.value.status_code == 400
    assert receiver.audit.rows(event=Event.WEBHOOK_REJECTED)


def test_a_missing_webhook_signature_is_refused(receiver: WebhookReceiver) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        receiver.handle(b'{"event":"payment.captured"}', "", "evt_003")


def test_the_signature_covers_the_exact_bytes_not_the_parsed_json(
    receiver: WebhookReceiver,
) -> None:
    """Re-serialising before hashing would compute a signature over different bytes.

    Razorpay signs the body it sent, whitespace and key order included.
    """
    original = b'{"event": "payment.captured",  "extra": 1}'
    reserialised = json.dumps(json.loads(original), separators=(",", ":")).encode()
    assert original != reserialised

    signature = _sign(original)
    assert verify_webhook_signature(original, signature, SECRET) is True
    assert verify_webhook_signature(reserialised, signature, SECRET) is False


def test_signature_comparison_is_constant_time() -> None:
    """`==` on a secret comparison leaks timing. Checked in the source."""
    source = (REPO_ROOT / "gateway" / "webhooks.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source


def test_an_empty_secret_never_validates_anything(receiver: WebhookReceiver) -> None:
    """A gateway with no configured secret must reject, not accept everything."""
    body = b'{"event":"payment.captured"}'
    assert verify_webhook_signature(body, _sign(body, ""), "") is False
    assert verify_webhook_signature(body, "", "") is False


def test_a_replayed_webhook_is_answered_once(receiver: WebhookReceiver) -> None:
    """Razorpay retries on any non-2xx, so duplicates are normal, not exceptional.

    Anyone who captures one valid body can also replay it verbatim.
    """
    body = json.dumps({"event": "payment.captured", "payload": {}}).encode()
    signature = _sign(body)

    first = receiver.handle(body, signature, "evt_replay")
    second = receiver.handle(body, signature, "evt_replay")
    third = receiver.handle(body, signature, "evt_replay")

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert third["duplicate"] is True
    assert len(receiver.audit.rows(event=Event.WEBHOOK_RECEIVED)) == 1
    assert len(receiver.audit.rows(event=Event.WEBHOOK_REPLAYED)) == 2


def test_deduplication_happens_after_signature_verification(
    receiver: WebhookReceiver,
) -> None:
    """Otherwise an unauthenticated caller could poison the seen-set and suppress
    a genuine webhook by claiming its event id first."""
    from fastapi import HTTPException

    body = json.dumps({"event": "payment.captured"}).encode()
    with pytest.raises(HTTPException):
        receiver.handle(body, "forged", "evt_poison")

    # The forged attempt must not have reserved the id.
    accepted = receiver.handle(body, _sign(body), "evt_poison")
    assert accepted["duplicate"] is False


def test_distinct_events_are_not_confused_for_replays(receiver: WebhookReceiver) -> None:
    body = json.dumps({"event": "payment.captured"}).encode()
    assert receiver.handle(body, _sign(body), "evt_a")["duplicate"] is False
    assert receiver.handle(body, _sign(body), "evt_b")["duplicate"] is False


def test_a_webhook_without_an_event_id_is_recorded_as_undeduplicable(
    receiver: WebhookReceiver,
) -> None:
    """The gap is made visible rather than silent."""
    body = json.dumps({"event": "payment.captured"}).encode()
    receiver.handle(body, _sign(body), "")
    row = receiver.audit.rows(event=Event.WEBHOOK_RECEIVED)[0]
    assert row.payload["deduplicable"] is False


def test_a_webhook_can_never_authorise_a_payment(wired: Gateway) -> None:
    """A verified webhook is information. Authorisation happened long before."""
    client = TestClient(create_app(wired))
    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"id": "pay_x", "amount": 9999999}}},
        }
    ).encode()
    before = wired.ledger.total_captured()
    client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": _sign(body), "X-Razorpay-Event-Id": "evt_x"},
    )
    assert wired.ledger.total_captured() == before, "a webhook moved no money"


def test_a_malformed_webhook_body_is_a_400_not_a_crash(receiver: WebhookReceiver) -> None:
    from fastapi import HTTPException

    body = b"not json at all"
    with pytest.raises(HTTPException) as excinfo:
        receiver.handle(body, _sign(body), "evt_bad")
    assert excinfo.value.status_code == 400


def test_a_webhook_with_an_unexpected_shape_does_not_crash(
    receiver: WebhookReceiver,
) -> None:
    """Untrusted input whose shape we do not control."""
    shapes: list[dict[str, Any]] = [
        {},
        {"payload": "a string"},
        {"payload": {"payment": 5}},
        {"payload": {}},
        {"payload": {"payment": {"entity": None}}},
        {"event": ["not", "a", "string"]},
    ]
    for body_obj in shapes:
        body = json.dumps(body_obj).encode()
        result = receiver.handle(body, _sign(body), f"evt_{hash(str(body_obj))}")
        assert result["received"] is True


# ===========================================================================
# Idempotency-key collision
# ===========================================================================


def test_the_idempotency_key_is_a_full_sha256_of_the_mandate_id() -> None:
    """Truncating it would trade collision resistance for nothing."""
    from gateway.payments import idempotency_key

    key = idempotency_key("pm_abc")
    assert key == hashlib.sha256(b"pm_abc").hexdigest()
    assert len(key) == 64


def test_mandate_ids_are_not_guessable_or_sequential() -> None:
    """A predictable mandate id would make idempotency keys predictable too."""
    from gateway.mandates import new_id

    ids = {new_id("pm") for _ in range(500)}
    assert len(ids) == 500, "no collisions"
    assert all(len(i.split("_", 1)[1]) == 16 for i in ids)


def test_two_different_mandates_cannot_share_a_key(wired: Gateway, fake_rail: FakeRail) -> None:
    first = confirmed_checkout(wired, "SF-RUN-001")
    second = confirmed_checkout(wired, "SF-APP-001")
    a = wired.merchant.initiate_payment(first["checkout_id"], signed_payment(wired, first))
    b = wired.merchant.initiate_payment(second["checkout_id"], signed_payment(wired, second))
    assert a["payment_receipt"]["idempotency_key"] != b["payment_receipt"]["idempotency_key"]
    assert fake_rail.captured_total() == inr(1299) + inr(899)


# ===========================================================================
# Secrets
# ===========================================================================


def test_no_secret_is_tracked_in_the_repository() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert ".env" not in tracked
    assert not [f for f in tracked if f.endswith((".pem", ".key", ".p12"))]


def test_the_env_example_contains_no_real_credential() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "rzp_live_" not in text
    assert "sk-ant-" not in text
    for line in text.splitlines():
        if line.startswith("RAZORPAY_KEY_ID="):
            value = line.split("=", 1)[1]
            assert set(value.removeprefix("rzp_test_")) == {"x"}, "must be a placeholder"
        if line.startswith(("RAZORPAY_KEY_SECRET=", "ANTHROPIC_API_KEY=")):
            assert line.split("=", 1)[1] == "", "must be empty"


def test_the_gitignore_covers_every_secret_and_state_artefact() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "*.pem", "*.db", "__pycache__", ".venv"):
        assert pattern in ignored, f"{pattern} is not gitignored"


def test_no_error_message_prints_a_whole_credential() -> None:
    """A key in a traceback ends up in a CI log, a paste, a screen share."""
    from gateway.config import ConfigurationError
    from gateway.razorpay_client import RazorpayRail

    with pytest.raises(ConfigurationError) as excinfo:
        RazorpayRail("rzp_live_THISWOULDBEAREALSECRET", "secret")
    message = str(excinfo.value)
    assert "THISWOULDBEAREALSECRET" not in message
    assert "secret" not in message.replace("RAZORPAY_KEY_SECRET", "")


def test_the_audit_trail_never_stores_a_key_or_a_full_mandate(
    wired: Gateway, fake_rail: FakeRail
) -> None:
    """Audit rows are for review. A private key or bearer token in one is a leak."""
    checkout = confirmed_checkout(wired)
    wired.merchant.initiate_payment(checkout["checkout_id"], signed_payment(wired, checkout))

    for row in wired.audit.rows():
        blob = json.dumps(row.payload) + (row.human_reason or "")
        assert "BEGIN PRIVATE KEY" not in blob
        assert "BEGIN EC PRIVATE KEY" not in blob
        for value in _strings_in(row.payload):
            assert not _looks_like_a_jws(value), f"an audit row carries a full JWS: {row.event}"


def _strings_in(value: object) -> list[str]:
    """Every string anywhere in a nested payload, not just the top level."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings_in(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings_in(v)]
    return []


def _looks_like_a_jws(value: str) -> bool:
    """Precise, so an English sentence with two full stops is not a false positive.

    A compact JWS is three base64url segments, contains no whitespace, and its
    header always begins ``eyJ`` — base64 of ``{"``.
    """
    return (
        value.startswith("eyJ")
        and value.count(".") == 2
        and not any(c.isspace() for c in value)
        and len(value) > 100
    )


def test_the_jws_detector_is_precise() -> None:
    """The detector above must not fire on prose, or the test above proves nothing."""
    assert not _looks_like_a_jws(
        "Approved Rs 1,299.00 to StrideFit: within the per-purchase limit and the "
        "remaining daily budget, bound to this checkout, first use of this token."
    )
    assert not _looks_like_a_jws("a" * 200)
    assert not _looks_like_a_jws("eyJ" + "a" * 200)  # no dots
    assert _looks_like_a_jws("eyJ" + "a" * 60 + "." + "b" * 60 + "." + "c" * 60)


# ===========================================================================
# The Trusted Surface as an attack surface
# ===========================================================================


def test_the_agent_cannot_approve_a_hold_over_http(wired: Gateway) -> None:
    """The only way to approve is a form POST; there is no agent-reachable API."""
    client = TestClient(create_app(wired))
    cart = wired.merchant.assemble_cart([{"sku": "SF-RUN-004", "qty": 1}])["cart"]
    checkout = wired.merchant.create_checkout(cart["cart_id"])
    held = wired.merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)
    hold_id = held["hold_id"]

    # No JSON approval endpoint exists.
    assert client.put(f"/trusted-surface/{hold_id}").status_code in {404, 405}
    assert client.post(f"/trusted-surface/{hold_id}").status_code in {404, 405}
    assert client.post(f"/trusted-surface/{hold_id}/status").status_code == 405
    assert client.post(f"/trusted-surface/{hold_id}/approve").status_code == 404

    # And the status endpoint hands out no mandate while pending.
    body = client.get(f"/trusted-surface/{hold_id}/status").json()
    assert body["payment_mandate_jws"] is None


def test_the_status_endpoint_leaks_no_key_material(wired: Gateway) -> None:
    client = TestClient(create_app(wired))
    cart = wired.merchant.assemble_cart([{"sku": "SF-RUN-004", "qty": 1}])["cart"]
    checkout = wired.merchant.create_checkout(cart["cart_id"])
    held = wired.merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)
    wired.trusted_surface.decide(held["hold_id"], approve=True)

    raw = client.get(f"/trusted-surface/{held['hold_id']}/status").text
    assert "PRIVATE KEY" not in raw
    assert '"d":' not in raw, "a JWK private scalar would appear under the key d"


def test_an_unknown_hold_id_does_not_confirm_or_deny_existence(wired: Gateway) -> None:
    client = TestClient(create_app(wired))
    response = client.get("/trusted-surface/gate_guessed")
    assert response.status_code == 404
    assert "gate_guessed" not in response.text or "no such" in response.text.lower()


# ===========================================================================
# Secret scanning over the whole git history
#
# The naive grep the review asked for — `git log -p | grep -iE
# "key_id|key_secret|sk-|rzp_"` — is NOT empty, and cannot be for a project that
# names these variables at all. It matches `RAZORPAY_KEY_ID` in .env.example,
# `key_secret` as a parameter name, `rzp_errors` (the SDK's module alias),
# documentation prose, and the deliberately-fake fixtures used to prove the
# live-key guard rejects them.
#
# So the useful check is not "does the word appear" but "does a CREDENTIAL
# appear". That is what this does: it looks for credential-*shaped* values and
# maintains an explicit, commented allowlist of the known-fake strings, the way a
# real secret scanner does.
# ===========================================================================

#: Deliberately fake values that appear in tests and documentation. Every one is
#: listed here so that "the scan is clean" cannot be achieved by a broad pattern
#: that would also hide a real key.
KNOWN_FAKE_CREDENTIALS = {
    # .env.example placeholders — all-x, obviously not a key.
    "rzp_test_xxxxxxxxxxxxxx",
    "rzp_test_XXXXXXXXXXXXXX",
    # tests/test_config.py — proves the live-key guard refuses them.
    "rzp_live_realmoney",
    "rzp_live_SUPERSECRETVALUE1234",
    # tests/test_config.py — proves the parser and the empty-secret guard.
    "rzp_test_abc",
    "rzp_test_abc123",
    # tests/test_security.py — proves an error message never echoes a whole key.
    "rzp_live_THISWOULDBEAREALSECRET",
}

#: Shapes that would indicate a real credential.
CREDENTIAL_PATTERNS = (
    r"rzp_live_[A-Za-z0-9]{6,}",
    r"rzp_test_[A-Za-z0-9]{6,}",
    r"sk-ant-[A-Za-z0-9_\-]{10,}",
    r"sk-[A-Za-z0-9]{20,}",
)


def _git_history() -> str:
    return subprocess.run(
        ["git", "log", "-p", "--all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_no_real_credential_appears_anywhere_in_git_history() -> None:
    """Every credential-shaped string in history must be a known fake.

    Scans the full history rather than the working tree, because a secret
    committed and then deleted is still a leaked secret.
    """
    import re

    history = _git_history()
    found: set[str] = set()
    for pattern in CREDENTIAL_PATTERNS:
        found.update(re.findall(pattern, history))

    real = found - KNOWN_FAKE_CREDENTIALS
    assert real == set(), f"credential-shaped strings in git history: {sorted(real)}"


def test_the_allowlist_only_contains_values_that_are_actually_fake() -> None:
    """An allowlist is only safe if every entry is obviously not a real key.

    Guards against the failure mode where somebody silences the scanner above by
    adding a genuine credential to the list.
    """
    for value in KNOWN_FAKE_CREDENTIALS:
        body = value.split("_", 2)[-1]
        looks_like_a_placeholder = (
            set(body.lower()) == {"x"}
            or "realmoney" in body.lower()
            or "secret" in body.lower()
            or body.lower().startswith("abc")
        )
        assert looks_like_a_placeholder, f"{value!r} does not look obviously fake"


def test_the_scanner_would_actually_catch_a_real_key() -> None:
    """A scanner nobody has tested against a real-shaped secret proves nothing.

    The bait is assembled at runtime rather than written as a literal. Writing it
    out would put a credential-shaped string into git history — which the scanner
    above would then correctly flag, and the usual "fix" for that is to widen the
    allowlist until the scanner is useless. It caught exactly this when the test
    was first written.
    """
    import re

    planted = " ".join(
        [
            "rzp_" + "live_" + "A1b2C3d4E5f6G7h8",
            "sk-" + "ant-" + "api03-Zx9YwVuTsRqPoNmLkJiHgF",
        ]
    )
    found: set[str] = set()
    for pattern in CREDENTIAL_PATTERNS:
        found.update(re.findall(pattern, planted))
    assert len(found - KNOWN_FAKE_CREDENTIALS) == 2, "the scanner missed a planted credential"


def test_the_working_tree_env_file_is_never_committed() -> None:
    """`.env` is where a reviewer will put their real key. It must stay untracked."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert ".env" not in tracked
    ever = subprocess.run(
        ["git", "log", "--all", "--diff-filter=A", "--name-only", "--pretty=format:"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert ".env" not in ever, ".env was committed at some point in history"


# ===========================================================================
# The demo seeding on the hosted deployment
# ===========================================================================


def test_demo_seeding_is_off_by_default(wired: Gateway) -> None:
    """`make serve` locally must be unchanged by a hosted-demo convenience."""
    from gateway.app import SEED_ENV_VAR, create_app

    assert os.environ.get(SEED_ENV_VAR) is None
    create_app(wired)
    assert wired.trusted_surface.pending() == []


def test_demo_seeding_raises_a_hold_but_grants_no_authority(
    wired: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeding must never be a back door.

    A hold is a *question*, not an answer. Raising one authorises nothing: the
    mandate only exists once a human decides on the page.
    """
    from gateway.app import SEED_ENV_VAR, create_app

    monkeypatch.setenv(SEED_ENV_VAR, "1")
    create_app(wired)

    pending = wired.trusted_surface.pending()
    assert len(pending) == 1
    held = pending[0]
    assert held.status == "pending"
    assert held.payment_mandate_jws is None, "seeding must not mint a mandate"
    assert held.checkout_mandate_jws is None
    assert wired.ledger.total_captured() == 0, "seeding must not move money"
    assert isinstance(wired.rail, FakeRail)
    assert wired.rail.calls == [], "seeding must not reach the rail"


def test_demo_seeding_cannot_stop_the_service_booting(
    wired: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoration must not be able to take the gateway down."""
    from gateway.app import SEED_ENV_VAR, create_app, seed_demo_hold

    monkeypatch.setenv(SEED_ENV_VAR, "1")
    wired.catalog.products.clear()  # the seed SKU no longer exists

    assert seed_demo_hold(wired) is None
    app = create_app(wired)  # must still build
    assert app is not None
