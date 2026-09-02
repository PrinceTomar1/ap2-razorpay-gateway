"""Durable state the verifier consults but never mutates.

Three things live here, all in SQLite:

1. **Spend ledger.** How much has already been spent under one open Payment
   Mandate. ``payment.budget`` is the only AP2 constraint that cannot be decided
   from the mandate alone, and this is the "accumulated total" its evaluation
   algorithm refers to.
2. **Nonce registry.** Every closed mandate carries a single-use nonce. Once a
   nonce has been accepted it is burned *and attributed to the mandate that burned
   it*, so that re-presenting the same mandate is an idempotent retry while a
   different mandate reusing that nonce is a replay.
3. **Idempotency store.** ``sha256(payment_mandate.id)`` → the terminal receipt,
   plus a short-lived *attempt lease*. The receipt makes a duplicate submit safe
   after the fact; the lease makes two *simultaneous* submits safe, which the
   receipt alone cannot — both would read "no receipt yet" and both would charge.

The read/write split matters. :class:`LedgerView` is the read-only protocol
gateway/verify.py depends on, so the verifier is a *pure function of its inputs
plus this view* — it cannot accidentally record a spend as a side effect of
deciding whether one is allowed. Only gateway/payments.py holds the mutating
:class:`Ledger`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from gateway.db import Database
from gateway.mandates import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_ledger (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    open_mandate_id   TEXT NOT NULL,
    payment_mandate_id TEXT NOT NULL UNIQUE,
    amount            INTEGER NOT NULL,
    currency          TEXT NOT NULL,
    payee             TEXT NOT NULL,
    ts                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_ledger_open_idx ON spend_ledger(open_mandate_id);

CREATE TABLE IF NOT EXISTS nonce_registry (
    nonce              TEXT PRIMARY KEY,
    payment_mandate_id TEXT NOT NULL,
    ts                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    idempotency_key    TEXT PRIMARY KEY,
    payment_mandate_id TEXT NOT NULL,
    status             TEXT NOT NULL,   -- in_flight | captured | failed
    receipt_jws        TEXT,
    receipt_json       TEXT,
    order_ids          TEXT NOT NULL DEFAULT '[]',
    attempts           INTEGER NOT NULL DEFAULT 0,
    -- Attempt lease. Held for the duration of one payment attempt so that two
    -- concurrent presentations of the same mandate cannot both create an order.
    -- Expiry-based, so a crashed holder does not wedge the key forever.
    lease_expires      TEXT,
    created_ts         TEXT NOT NULL,
    updated_ts         TEXT NOT NULL
);
"""

#: Statuses the idempotency store can hold. `captured` and `failed` are terminal:
#: once a key reaches either, every later presentation of that mandate returns the
#: stored receipt and no new order is ever created.
TERMINAL_STATUSES = frozenset({"captured", "failed"})


class LedgerView(Protocol):
    """The read-only surface gateway/verify.py is allowed to see."""

    def spent_under(self, open_mandate_id: str) -> int:
        """Total paise already committed under this open Payment Mandate."""
        ...

    def nonce_owner(self, nonce: str) -> str | None:
        """Which Payment Mandate burned this nonce, if any.

        Ownership rather than mere presence, because "the same mandate again" and
        "a different mandate reusing a burned token" are opposite situations. The
        first is an idempotent retry we must allow; the second is a replay we must
        refuse. A boolean cannot tell them apart.
        """
        ...


@dataclass(frozen=True)
class IdempotencyRecord:
    idempotency_key: str
    payment_mandate_id: str
    status: str
    receipt_jws: str | None
    receipt: dict[str, Any] | None
    order_ids: list[str]
    attempts: int

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class Ledger:
    """The mutating ledger. Held by the payment processor, never by the verifier."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.db.executescript(SCHEMA)

    # -- LedgerView (read-only) --------------------------------------------

    def spent_under(self, open_mandate_id: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM spend_ledger WHERE open_mandate_id = ?",
            (open_mandate_id,),
        )
        return int(row["total"]) if row else 0

    def nonce_owner(self, nonce: str) -> str | None:
        row = self.db.query_one(
            "SELECT payment_mandate_id FROM nonce_registry WHERE nonce = ?", (nonce,)
        )
        return str(row["payment_mandate_id"]) if row else None

    def nonce_seen(self, nonce: str) -> bool:
        return self.nonce_owner(nonce) is not None

    # -- mutations ----------------------------------------------------------

    def burn_nonce(self, nonce: str, payment_mandate_id: str) -> bool:
        """Consume a nonce. Returns False if it was already burned.

        The INSERT is the check: relying on the primary key rather than a
        read-then-write means two concurrent presentations of the same mandate
        cannot both see "unused".
        """
        try:
            self.db.execute(
                "INSERT INTO nonce_registry (nonce, payment_mandate_id, ts) VALUES (?, ?, ?)",
                (nonce, payment_mandate_id, utcnow().isoformat()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def record_spend(
        self,
        *,
        open_mandate_id: str,
        payment_mandate_id: str,
        amount: int,
        currency: str,
        payee: str,
    ) -> None:
        """Add a captured amount to the accumulated total for ``payment.budget``.

        Called only after a capture. A declined payment moves no money and must
        not consume budget — charging an agent's budget for a bank decline would
        let an attacker exhaust a user's daily limit without ever taking a rupee.
        """
        self.db.execute(
            "INSERT OR IGNORE INTO spend_ledger"
            " (open_mandate_id, payment_mandate_id, amount, currency, payee, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (open_mandate_id, payment_mandate_id, amount, currency, payee, utcnow().isoformat()),
        )

    def total_captured(self) -> int:
        row = self.db.query_one("SELECT COALESCE(SUM(amount), 0) AS total FROM spend_ledger")
        return int(row["total"]) if row else 0

    def spends(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM spend_ledger ORDER BY id")]

    # -- idempotency --------------------------------------------------------

    def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        raw = self.db.query_one("SELECT * FROM idempotency WHERE idempotency_key = ?", (key,))
        if raw is None:
            return None
        return IdempotencyRecord(
            idempotency_key=str(raw["idempotency_key"]),
            payment_mandate_id=str(raw["payment_mandate_id"]),
            status=str(raw["status"]),
            receipt_jws=raw["receipt_jws"],
            receipt=json.loads(raw["receipt_json"]) if raw["receipt_json"] else None,
            order_ids=json.loads(raw["order_ids"]),
            attempts=int(raw["attempts"]),
        )

    def claim(self, key: str, payment_mandate_id: str) -> IdempotencyRecord:
        """Claim an idempotency key, or return the existing record for it.

        One immediate transaction so two concurrent presentations of the same
        mandate cannot both create a fresh ``in_flight`` row.
        """
        now = utcnow().isoformat()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM idempotency WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO idempotency (idempotency_key, payment_mandate_id, status,"
                    " order_ids, attempts, created_ts, updated_ts)"
                    " VALUES (?, ?, 'in_flight', '[]', 0, ?, ?)",
                    (key, payment_mandate_id, now, now),
                )
        record = self.get_idempotency(key)
        assert record is not None  # just inserted or already present
        return record

    def acquire_attempt_lease(self, key: str, *, lease_seconds: float) -> bool:
        """Take exclusive right to attempt this key. Returns False if held elsewhere.

        Claiming the key is not enough on its own. Two requests arriving at the
        same instant both find a non-terminal record, and without this both would
        go on to create an order — one mandate, two charges. The conditional
        UPDATE inside a ``BEGIN IMMEDIATE`` transaction is what serialises them,
        and it does so across *processes*, not merely across threads.

        The lease expires so that a holder that crashes mid-attempt does not wedge
        the key permanently. A successor that takes over an expired lease still
        runs the capture probe before creating anything, so the takeover cannot
        double-charge either.
        """
        now = utcnow()
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE idempotency SET lease_expires = ?, updated_ts = ?"
                " WHERE idempotency_key = ? AND status = 'in_flight'"
                " AND (lease_expires IS NULL OR lease_expires <= ?)",
                (expiry, now.isoformat(), key, now.isoformat()),
            )
            return cursor.rowcount == 1

    def release_attempt_lease(self, key: str) -> None:
        """Give up the lease. Always in a ``finally``."""
        self.db.execute(
            "UPDATE idempotency SET lease_expires = NULL WHERE idempotency_key = ?", (key,)
        )

    def note_order(self, key: str, order_id: str) -> None:
        """Record an order created under this idempotency key.

        Every order id ever minted for a mandate is kept, so recovery can ask the
        rail about *all* of them before creating another. That is the guard that
        makes "retry" safe: see gateway/recovery.py.
        """
        record = self.get_idempotency(key)
        if record is None:
            raise KeyError(f"unclaimed idempotency key {key!r}")
        if order_id in record.order_ids:
            return
        orders = [*record.order_ids, order_id]
        self.db.execute(
            "UPDATE idempotency SET order_ids = ?, attempts = attempts + 1, updated_ts = ?"
            " WHERE idempotency_key = ?",
            (json.dumps(orders), utcnow().isoformat(), key),
        )

    def finalise(
        self, key: str, *, status: str, receipt_jws: str, receipt: dict[str, Any]
    ) -> IdempotencyRecord:
        """Write the terminal receipt for an idempotency key.

        Refuses to overwrite a terminal record. A second capture landing on a key
        that already captured is not a race to resolve gracefully — it is the
        double charge this whole store exists to prevent, and it should be loud.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"{status!r} is not a terminal status")
        existing = self.get_idempotency(key)
        if existing is not None and existing.is_terminal:
            raise DoubleFinalisationError(
                f"idempotency key {key} is already terminal ({existing.status}); "
                f"refusing to overwrite it with {status}"
            )
        self.db.execute(
            "UPDATE idempotency SET status = ?, receipt_jws = ?, receipt_json = ?, updated_ts = ?"
            " WHERE idempotency_key = ?",
            (status, receipt_jws, json.dumps(receipt), utcnow().isoformat(), key),
        )
        record = self.get_idempotency(key)
        assert record is not None
        return record


class DoubleFinalisationError(RuntimeError):
    """Two terminal outcomes for one Payment Mandate. Should be impossible."""


class InMemoryLedgerView:
    """A hand-built :class:`LedgerView` for unit tests of gateway/verify.py.

    Lets a test say "pretend ₹4,800 has already been spent" without standing up a
    database, which keeps the verifier's tests about the verifier.
    """

    def __init__(
        self, spent: dict[str, int] | None = None, nonces: dict[str, str] | None = None
    ) -> None:
        self._spent = dict(spent or {})
        self._nonces = dict(nonces or {})

    def spent_under(self, open_mandate_id: str) -> int:
        return self._spent.get(open_mandate_id, 0)

    def nonce_owner(self, nonce: str) -> str | None:
        return self._nonces.get(nonce)

    def nonce_seen(self, nonce: str) -> bool:
        return nonce in self._nonces

    def burn_nonce(self, nonce: str, payment_mandate_id: str) -> bool:
        if nonce in self._nonces:
            return False
        self._nonces[nonce] = payment_mandate_id
        return True

    def add_spend(self, open_mandate_id: str, amount: int) -> None:
        self._spent[open_mandate_id] = self._spent.get(open_mandate_id, 0) + amount
