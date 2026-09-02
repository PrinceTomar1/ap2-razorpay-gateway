"""The audit trail is tamper-evident, and here is the proof.

A hash chain nobody has tried to break is a hope. These tests break it four
different ways — edit a payload, edit a reason, delete a row, splice in a forged
row — and assert that :meth:`AuditLog.verify_chain` names the damaged row every
time.
"""

from __future__ import annotations

import sqlite3
from itertools import pairwise

import pytest

from gateway.audit import GENESIS_HASH, AuditLog, Event, render_log, row_hash
from gateway.db import Database


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog(Database())


def _seed(audit: AuditLog, n: int = 5) -> None:
    for i in range(n):
        audit.append(
            actor="merchant_payment_processor",
            event=Event.PAYMENT_ATTEMPT,
            payload={"attempt": i, "amount": 129900 + i, "order_id": f"order_{i}"},
            human_reason=f"attempt {i} for order_{i}",
        )


def _drop_append_only_triggers(db: Database) -> None:
    """Simulate an operator with write access to the database file.

    The triggers stop casual tampering. The chain is what catches determined
    tampering, and that is what these tests are actually about — so we take the
    triggers out of the way first.
    """
    db.executescript(
        "DROP TRIGGER IF EXISTS audit_log_no_update; DROP TRIGGER IF EXISTS audit_log_no_delete;"
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_empty_chain_verifies(audit: AuditLog) -> None:
    result = audit.verify_chain()
    assert result.ok
    assert result.rows_checked == 0


def test_chain_verifies_after_appends(audit: AuditLog) -> None:
    _seed(audit, 20)
    result = audit.verify_chain()
    assert result.ok, result.reason
    assert result.rows_checked == 20
    assert bool(result) is True


def test_first_row_anchors_to_genesis(audit: AuditLog) -> None:
    row = audit.append("merchant", Event.CART_ASSEMBLED, {"cart_id": "c1"})
    assert row.prev_hash == GENESIS_HASH
    assert row.id == 1


def test_each_row_links_to_its_predecessor(audit: AuditLog) -> None:
    _seed(audit, 4)
    rows = audit.rows()
    for previous, current in pairwise(rows):
        assert current.prev_hash == previous.hash


def test_row_hash_is_reproducible_by_a_third_party(audit: AuditLog) -> None:
    """A reviewer with the row contents can recompute the hash themselves."""
    row = audit.append("verifier", Event.DECISION, {"outcome": "ALLOW"}, "within all bounds")
    recomputed = row_hash(
        row.prev_hash, row.actor, row.event, row.payload, row.ts, row.human_reason
    )
    assert recomputed == row.hash


def test_payload_key_order_does_not_change_the_hash() -> None:
    a = row_hash(GENESIS_HASH, "x", "e", {"b": 2, "a": 1}, "2026-01-01T00:00:00+00:00", "why")
    b = row_hash(GENESIS_HASH, "x", "e", {"a": 1, "b": 2}, "2026-01-01T00:00:00+00:00", "why")
    assert a == b


# ---------------------------------------------------------------------------
# Tampering
# ---------------------------------------------------------------------------


def test_verify_chain_catches_an_edited_payload(audit: AuditLog) -> None:
    """The headline claim: change one rupee in one row and the chain says so."""
    _seed(audit, 5)
    assert audit.verify_chain().ok

    _drop_append_only_triggers(audit.db)
    audit.db.execute(
        "UPDATE audit_log SET payload_json = ? WHERE id = 3",
        ('{"amount":1,"attempt":2,"order_id":"order_2"}',),
    )

    result = audit.verify_chain()
    assert not result.ok
    assert result.broken_at == 3
    assert result.reason is not None
    assert "edited after it was written" in result.reason


def test_verify_chain_catches_an_edited_human_reason(audit: AuditLog) -> None:
    """The 'why' column is inside the commitment, not decoration beside it.

    Rewriting the explanation of a payment without changing its numbers is
    exactly the tamper a dispute would turn on.
    """
    _seed(audit, 3)
    _drop_append_only_triggers(audit.db)
    audit.db.execute("UPDATE audit_log SET human_reason = 'nothing to see here' WHERE id = 2")

    result = audit.verify_chain()
    assert not result.ok
    assert result.broken_at == 2
    assert result.reason is not None
    assert "edited after it was written" in result.reason


@pytest.mark.parametrize("column", ["actor", "event", "ts"])
def test_verify_chain_catches_edits_to_every_committed_column(audit: AuditLog, column: str) -> None:
    _seed(audit, 3)
    _drop_append_only_triggers(audit.db)
    audit.db.execute(f"UPDATE audit_log SET {column} = 'tampered' WHERE id = 2")
    result = audit.verify_chain()
    assert not result.ok
    assert result.broken_at == 2


def test_verify_chain_catches_a_deleted_row(audit: AuditLog) -> None:
    _seed(audit, 6)
    _drop_append_only_triggers(audit.db)
    audit.db.execute("DELETE FROM audit_log WHERE id = 4")

    result = audit.verify_chain()
    assert not result.ok
    assert result.broken_at == 5  # the row whose link now dangles
    assert result.reason is not None
    assert "deleted, reordered or inserted" in result.reason


def test_verify_chain_catches_a_spliced_forged_row(audit: AuditLog) -> None:
    """An attacker who recomputes one row's hash still breaks the next link."""
    _seed(audit, 4)
    _drop_append_only_triggers(audit.db)
    rows = audit.rows()
    victim = rows[1]
    forged_payload = {"attempt": 1, "amount": 1, "order_id": "order_1"}
    forged_hash = row_hash(
        victim.prev_hash,
        victim.actor,
        victim.event,
        forged_payload,
        victim.ts,
        victim.human_reason,
    )
    audit.db.execute(
        "UPDATE audit_log SET payload_json = ?, hash = ? WHERE id = ?",
        ('{"amount":1,"attempt":1,"order_id":"order_1"}', forged_hash, victim.id),
    )

    result = audit.verify_chain()
    assert not result.ok
    # Row 2 now hashes consistently, so the break surfaces at row 3, whose
    # prev_hash still points at the original row-2 hash.
    assert result.broken_at == 3
    assert result.reason is not None
    assert "deleted, reordered or inserted" in result.reason


def test_verify_chain_catches_a_truncated_tail_only_via_the_tip(audit: AuditLog) -> None:
    """Deleting the LAST row leaves a self-consistent chain.

    This is the honest limit of a self-contained hash chain: truncation from the
    end is undetectable without an external anchor. We publish the tip hash so a
    third party can, which is why `tip_hash()` exists — and it is recorded in
    LIMITATIONS.md.
    """
    _seed(audit, 5)
    anchored_tip = audit.tip_hash()
    _drop_append_only_triggers(audit.db)
    audit.db.execute("DELETE FROM audit_log WHERE id = 5")

    assert audit.verify_chain().ok, "a truncated chain still verifies internally"
    assert audit.tip_hash() != anchored_tip, "but the tip no longer matches the anchor"


# ---------------------------------------------------------------------------
# Append-only enforcement
# ---------------------------------------------------------------------------


def test_the_table_refuses_updates(audit: AuditLog) -> None:
    _seed(audit, 1)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        audit.db.execute("UPDATE audit_log SET actor = 'x' WHERE id = 1")


def test_the_table_refuses_deletes(audit: AuditLog) -> None:
    _seed(audit, 1)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        audit.db.execute("DELETE FROM audit_log WHERE id = 1")


# ---------------------------------------------------------------------------
# Querying and rendering
# ---------------------------------------------------------------------------


def test_find_matches_on_payload_fields(audit: AuditLog) -> None:
    _seed(audit, 5)
    hits = audit.find(Event.PAYMENT_ATTEMPT, order_id="order_3")
    assert len(hits) == 1
    assert hits[0].payload["attempt"] == 3


def test_find_returns_empty_when_nothing_matches(audit: AuditLog) -> None:
    _seed(audit, 2)
    assert audit.find(Event.PAYMENT_CAPTURED) == []
    assert audit.find(Event.PAYMENT_ATTEMPT, order_id="nope") == []


def test_count_by_event(audit: AuditLog) -> None:
    _seed(audit, 3)
    audit.append("verifier", Event.DECISION, {"outcome": "ALLOW"})
    assert audit.count() == 4
    assert audit.count(Event.PAYMENT_ATTEMPT) == 3
    assert audit.count(Event.DECISION) == 1


def test_rows_round_trip_the_payload(audit: AuditLog) -> None:
    audit.append("merchant", Event.CART_ASSEMBLED, {"items": [{"sku": "a", "qty": 2}], "n": 1})
    row = audit.rows()[0]
    assert row.payload == {"items": [{"sku": "a", "qty": 2}], "n": 1}


def test_render_log_is_plain_text_without_colour(audit: AuditLog) -> None:
    audit.append("verifier", Event.DECISION, {"outcome": "ALLOW"}, "within the standing limit")
    rendered = render_log(audit.rows(), colour=False)
    assert "verifier.decision" in rendered
    assert "within the standing limit" in rendered
    assert "\033[" not in rendered


def test_render_log_colours_when_asked(audit: AuditLog) -> None:
    audit.append("mpp", Event.PAYMENT_CAPTURED, {"amount": 1})
    assert "\033[" in render_log(audit.rows(), colour=True)


def test_timestamps_parse_back_to_aware_datetimes(audit: AuditLog) -> None:
    row = audit.append("merchant", Event.CART_ASSEMBLED, {})
    assert row.timestamp.tzinfo is not None
