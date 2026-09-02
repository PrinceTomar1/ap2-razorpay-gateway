"""Durable state the verifier consults but never mutates.

Three things live here, all in SQLite:

1. **Spend ledger.** How much has already been spent under one open Payment
   Mandate. ``payment.budget`` is the only AP2 constraint that cannot be decided
   from the mandate alone, and this is the "accumulated total" its evaluation
   algorithm refers to.
2. **Nonce registry.** Every closed mandate carries a single-use nonce. Once a
   nonce has been accepted it is burned, and a second presentation of it is a
   replay.
3. **Idempotency store.** ``sha256(payment_mandate.id)`` → the terminal receipt.
   This is the thing that makes a duplicate submit safe.

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

    def nonce_seen(self, nonce: str) -> bool:
        """Has this single-use nonce already been accepted?"""
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

    def nonce_seen(self, nonce: str) -> bool:
        return (
            self.db.query_one("SELECT 1 FROM nonce_registry WHERE nonce = ?", (nonce,)) is not None
        )

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
        mandate cannot both create a fresh ``in_flight`` row and then both go on
        to create an order.
        """
        now = utcnow().isoformat()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency WHERE idempotency_key = ?", (key,)
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

    def __init__(self, spent: dict[str, int] | None = None, nonces: set[str] | None = None) -> None:
        self._spent = dict(spent or {})
        self._nonces = set(nonces or set())

    def spent_under(self, open_mandate_id: str) -> int:
        return self._spent.get(open_mandate_id, 0)

    def nonce_seen(self, nonce: str) -> bool:
        return nonce in self._nonces

    def burn_nonce(self, nonce: str, payment_mandate_id: str) -> bool:
        if nonce in self._nonces:
            return False
        self._nonces.add(nonce)
        return True

    def add_spend(self, open_mandate_id: str, amount: int) -> None:
        self._spent[open_mandate_id] = self._spent.get(open_mandate_id, 0) + amount
