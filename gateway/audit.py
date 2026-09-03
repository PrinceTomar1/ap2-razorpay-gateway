"""The audit trail: an append-only, hash-chained log of every money action.

Track 01's bar is "every money action explainable, bounded and gated. Show the
audit trail." This module is the audit trail. It is deliberately boring:

    hash_n = sha256(hash_{n-1} + canonical_json(
        {actor, event, payload, ts, human_reason}))

Each row commits to its predecessor, so editing row 4 invalidates rows 4..N, and
deleting row 4 breaks the link between 3 and 5. :meth:`AuditLog.verify_chain`
finds either in one pass, and reports *which* row broke and how. That is the
difference between "we have logs" and "we can prove the logs are intact".

Two further properties worth naming:

* **The database refuses UPDATE and DELETE.** Triggers, not convention. An
  operator with a sqlite3 prompt still has to consciously drop a trigger to
  tamper — and the chain then catches what they did anyway. tests/test_audit_chain.py
  does exactly that, because a tamper-evidence claim you have not tried to break
  is a hope, not a property.
* **`human_reason` is a column, not a log line.** Every row carries one plain
  English sentence explaining why the action happened. It is supplied by the
  caller. When it comes from a language model (llm/reason.py) and the model is
  unavailable, the caller writes a deterministic template instead and the write
  still happens — see the module docstring of llm/reason.py. The audit log never
  depends on an LLM being reachable.

This module imports nothing from llm/. It is a ledger, not a narrator.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gateway.db import Database
from gateway.mandates import canonical_json, utcnow

#: The chain's anchor. Row 1's ``prev_hash``.
GENESIS_HASH = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL,
    actor        TEXT NOT NULL,
    event        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    human_reason TEXT
);
CREATE INDEX IF NOT EXISTS audit_log_event_idx ON audit_log(event);

-- Append-only at the storage layer, not merely by convention.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
"""


def row_hash(
    prev_hash: str,
    actor: str,
    event: str,
    payload: dict[str, Any],
    ts: str,
    human_reason: str | None = None,
) -> str:
    """The chain link for one row.

    Canonical JSON (sorted keys, no incidental whitespace) so the hash a reviewer
    recomputes next month on another machine matches the one written today.

    ``human_reason`` is inside the commitment. It would have been easier to leave
    the prose out and hash only the machine-readable payload, but the "why"
    column is what a human actually reads when reviewing a disputed payment — an
    audit trail where the numbers are tamper-evident and the explanation beside
    them is freely editable is not much of an audit trail.
    """
    body = canonical_json(
        {
            "actor": actor,
            "event": event,
            "payload": payload,
            "ts": ts,
            "human_reason": human_reason,
        }
    )
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditRow:
    id: int
    ts: str
    prev_hash: str
    hash: str
    actor: str
    event: str
    payload: dict[str, Any]
    human_reason: str | None

    @property
    def timestamp(self) -> datetime:
        return datetime.fromisoformat(self.ts)


@dataclass(frozen=True)
class ChainVerification:
    """The result of walking the whole chain."""

    ok: bool
    rows_checked: int
    broken_at: int | None = None
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------------------
# Event names
#
# Constants, not string literals, so that a test asserting "an audit row exists
# for this failure mode" and the code writing that row cannot drift apart.
# ---------------------------------------------------------------------------


class Event:
    # Merchant role
    CART_ASSEMBLED = "merchant.cart_assembled"
    CHECKOUT_CREATED = "merchant.checkout_created"
    CHECKOUT_MANDATE_RECEIVED = "merchant.checkout_mandate_received"
    CHECKOUT_RECEIPT_ISSUED = "merchant.checkout_receipt_issued"
    CHECKOUT_UNRESOLVED = "merchant.checkout_unresolved_constraint"
    PRODUCT_NOT_FOUND = "merchant.product_not_found"
    STOCK_RECHECK_FAILED = "merchant.stock_recheck_failed"
    STOCK_DECREMENTED = "merchant.stock_decremented"

    # Verifier
    PAYMENT_MANDATE_RECEIVED = "merchant.payment_mandate_received"
    CHECK_RESULT = "verifier.check"
    DECISION = "verifier.decision"
    MANDATE_REJECTED = "verifier.mandate_rejected"

    # Payment processor
    IDEMPOTENT_REPLAY = "mpp.idempotent_replay"
    PAYMENT_ATTEMPT = "mpp.payment_attempt"
    PAYMENT_CAPTURED = "mpp.payment_captured"
    PAYMENT_DECLINED = "mpp.payment_declined"
    PAYMENT_RECEIPT_ISSUED = "mpp.payment_receipt_issued"
    ORDER_CREATED = "mpp.order_created"
    WEBHOOK_RECEIVED = "mpp.webhook_received"
    WEBHOOK_REJECTED = "mpp.webhook_signature_rejected"
    WEBHOOK_REPLAYED = "mpp.webhook_replayed"

    # Recovery
    RECOVERY_STARTED = "recovery.started"
    RECOVERY_METHOD_FALLBACK = "recovery.method_fallback"
    RECOVERY_BACKOFF = "recovery.backoff"
    RECOVERY_SUCCEEDED = "recovery.succeeded"
    RECOVERY_EXHAUSTED = "recovery.exhausted"
    RECOVERY_NOT_RETRYABLE = "recovery.not_retryable"
    RECOVERY_ABORTED_PRIOR_CAPTURE = "recovery.aborted_prior_capture"
    CIRCUIT_OPENED = "recovery.circuit_opened"
    CIRCUIT_DEFERRED = "recovery.circuit_deferred"
    CIRCUIT_CLOSED = "recovery.circuit_closed"
    RAIL_TIMEOUT = "recovery.rail_timeout"

    # Trusted Surface
    GATE_REQUESTED = "trusted_surface.gate_requested"
    GATE_APPROVED = "trusted_surface.gate_approved"
    GATE_DENIED = "trusted_surface.gate_denied"
    GATE_EXPIRED = "trusted_surface.gate_expired"

    # Shopping agent
    AGENT_PLAN = "agent.plan"
    AGENT_REPLANNED = "agent.replanned"
    AGENT_ESCALATED = "agent.escalated"
    AGENT_GAVE_UP = "agent.gave_up"


class AuditLog:
    """Append-only hash chain over a SQLite table."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.db.executescript(SCHEMA)

    # -- writing ------------------------------------------------------------

    def append(
        self,
        actor: str,
        event: str,
        payload: dict[str, Any] | None = None,
        human_reason: str | None = None,
        *,
        ts: datetime | None = None,
    ) -> AuditRow:
        """Append one row and return it.

        The read of the previous hash and the write of the new row happen inside
        one immediate transaction: two concurrent appends must not both read the
        same tip and fork the chain.
        """
        payload = payload or {}
        stamp = (ts or utcnow()).isoformat()
        with self.db.transaction() as conn:
            tip = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev = tip["hash"] if tip else GENESIS_HASH
            digest = row_hash(prev, actor, event, payload, stamp, human_reason)
            cursor = conn.execute(
                "INSERT INTO audit_log (ts, prev_hash, hash, actor, event, payload_json,"
                " human_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (stamp, prev, digest, actor, event, canonical_json(payload), human_reason),
            )
            row_id = int(cursor.lastrowid or 0)
        return AuditRow(
            id=row_id,
            ts=stamp,
            prev_hash=prev,
            hash=digest,
            actor=actor,
            event=event,
            payload=payload,
            human_reason=human_reason,
        )

    # -- reading ------------------------------------------------------------

    def rows(self, *, event: str | None = None, limit: int | None = None) -> list[AuditRow]:
        sql = "SELECT * FROM audit_log"
        params: tuple[object, ...] = ()
        if event is not None:
            sql += " WHERE event = ?"
            params = (event,)
        sql += " ORDER BY id"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [self._to_row(r) for r in self.db.query(sql, params)]

    def find(self, event: str, **payload_match: Any) -> list[AuditRow]:
        """Rows for ``event`` whose payload contains every given key/value pair.

        The failure-mode tests use this to assert not just "something was logged"
        but "the right thing was logged, about the right transaction".
        """
        return [
            row
            for row in self.rows(event=event)
            if all(row.payload.get(k) == v for k, v in payload_match.items())
        ]

    def count(self, event: str | None = None) -> int:
        if event is None:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM audit_log")
        else:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM audit_log WHERE event = ?", (event,))
        return int(row["n"]) if row else 0

    def tip_hash(self) -> str:
        row = self.db.query_one("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
        return str(row["hash"]) if row else GENESIS_HASH

    @staticmethod
    def _to_row(raw: Any) -> AuditRow:
        return AuditRow(
            id=int(raw["id"]),
            ts=str(raw["ts"]),
            prev_hash=str(raw["prev_hash"]),
            hash=str(raw["hash"]),
            actor=str(raw["actor"]),
            event=str(raw["event"]),
            payload=json.loads(raw["payload_json"]),
            human_reason=raw["human_reason"],
        )

    # -- verifying ----------------------------------------------------------

    def verify_chain(self) -> ChainVerification:
        """Walk the chain from genesis and report the first break.

        Catches, in one pass:

        * an **edited** row — its stored hash no longer matches its contents;
        * a **deleted** row — the next row's ``prev_hash`` no longer matches the
          previous row's ``hash``;
        * a **forged** row appended with a recomputed hash but the wrong link.

        What it deliberately does *not* claim: this detects tampering, it does not
        prevent it. An attacker who can rewrite the whole table can recompute every
        hash. Preventing that needs an external anchor (publishing the tip hash,
        a notary, WORM storage) and is listed in LIMITATIONS.md.
        """
        previous = GENESIS_HASH
        checked = 0
        for raw in self.db.query("SELECT * FROM audit_log ORDER BY id"):
            row = self._to_row(raw)
            checked += 1
            if row.prev_hash != previous:
                return ChainVerification(
                    ok=False,
                    rows_checked=checked,
                    broken_at=row.id,
                    reason=(
                        f"row {row.id} links to {row.prev_hash[:12]}… but the previous row hashes "
                        f"to {previous[:12]}… — a row was deleted, reordered or inserted"
                    ),
                )
            expected = row_hash(
                row.prev_hash, row.actor, row.event, row.payload, row.ts, row.human_reason
            )
            if expected != row.hash:
                return ChainVerification(
                    ok=False,
                    rows_checked=checked,
                    broken_at=row.id,
                    reason=(
                        f"row {row.id} ({row.event}) stores hash {row.hash[:12]}… but its contents "
                        f"hash to {expected[:12]}… — the row was edited after it was written"
                    ),
                )
            previous = row.hash
        return ChainVerification(ok=True, rows_checked=checked)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_ACTOR_WIDTH = 22


def render_log(rows: list[AuditRow], *, colour: bool = True, compact: bool = True) -> str:
    """Pretty-print the audit log the way a terminal log reads.

    Used by `make demo`. The point is that a reviewer can scroll it and see the
    whole decision path — mandate in, every check, the decision, the gate, the
    payment attempts, the receipt — without opening the database.

    ``compact`` folds a run of *passing* verifier checks into one line. A clean
    purchase runs fourteen of them and printing each is noise; a **failing** check
    is never folded, because that is the line a reader is looking for.
    """

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if colour else text

    lines: list[str] = []
    passed_run: list[str] = []

    def flush_run() -> None:
        if not passed_run:
            return
        names = ", ".join(passed_run)
        lines.append(
            f"  {'':8}  {'verifier':<{_ACTOR_WIDTH}} "
            f"{paint(f'{len(passed_run)} checks passed', '32')}"
        )
        lines.append(f"           {paint('↳ ' + names, '90')}")
        passed_run.clear()

    for row in rows:
        if compact and row.event == Event.CHECK_RESULT and row.payload.get("passed") is True:
            passed_run.append(str(row.payload.get("check", "?")))
            continue
        flush_run()

        stamp = row.ts[11:19]
        event = row.event
        if event.startswith(("verifier.decision", "mpp.payment_captured", "recovery.succeeded")):
            tint = "32"  # green
        elif any(word in event for word in ("denied", "declined", "rejected", "failed")):
            tint = "31"  # red
        elif event.startswith(("recovery.", "trusted_surface.")):
            tint = "33"  # yellow
        else:
            tint = "37"  # grey
        lines.append(f"  {stamp}  {row.actor:<{_ACTOR_WIDTH}} {paint(event, tint)}")
        if row.human_reason:
            lines.append(f"           {paint('↳ ' + row.human_reason, '90')}")

    flush_run()
    return "\n".join(lines)
