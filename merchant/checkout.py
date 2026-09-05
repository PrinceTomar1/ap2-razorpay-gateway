"""Catalogue, carts and checkout state — the Merchant role's own bookkeeping.

The interesting part of this module is the smallest: :meth:`CheckoutStore.recheck`.
Everything else is ordinary retail plumbing.

The stock race
--------------
A cart is priced and stock-checked when the merchant signs the Checkout Mandate.
Then time passes — the agent gets the mandate signed, presents it, a payment is
attempted, a payment fails, recovery retries. Any of that can outlast the last
unit on the shelf. So stock and price are re-read *immediately before every
payment attempt*, against live state, and a mismatch stops the payment cleanly
rather than selling something we no longer have. That is failure mode 5, and it
is the reason the signed cart carries prices at all: without them there would be
nothing to compare against.

Stock is decremented on **capture**, never on cart assembly or checkout creation.
Reserving stock at checkout would be defensible in a real system with a
reservation TTL, but decrementing it before the money arrives means a failed
payment silently destroys inventory.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ap2_min.models import Cart, CartItem, inr, paise_to_inr_str
from gateway.mandates import new_id, utcnow

DEFAULT_SEED = Path(__file__).with_name("seed.json")


class CatalogError(Exception):
    """Base class for catalogue and cart problems, all with a stable code."""

    code = "catalog.error"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.detail}


class ProductNotFound(CatalogError):
    """Failure mode 7: the agent asked for a SKU that does not exist.

    Answered flatly and cheaply. No cart is built, no mandate is signed, the
    verifier never runs, and the agent gets a code it can re-plan against.
    """

    code = "product.not_found"


class OutOfStock(CatalogError):
    code = "product.out_of_stock"


class NotServiceable(CatalogError):
    code = "pincode.not_serviceable"


class MixedMerchantCart(CatalogError):
    """One Payment Mandate names one payee, so one cart means one merchant."""

    code = "cart.mixed_merchants"


@dataclass(frozen=True)
class Product:
    sku: str
    merchant_id: str
    merchant_name: str
    name: str
    category: str
    price: int  # paise
    sizes: tuple[str, ...]
    serviceable_pincodes: tuple[str, ...]
    return_days: int

    def as_dict(self, *, stock: int) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "name": self.name,
            "merchant_id": self.merchant_id,
            "merchant_name": self.merchant_name,
            "category": self.category,
            "price_inr": paise_to_inr_str(self.price),
            "price_paise": self.price,
            "stock": stock,
            "sizes": list(self.sizes),
            "serviceable_pincodes": list(self.serviceable_pincodes),
            "return_days": self.return_days,
        }


@dataclass(frozen=True)
class Merchant:
    id: str
    name: str
    city: str
    serviceable_pincodes: tuple[str, ...]
    return_days: int


class Catalog:
    """Products and live stock, loaded from seed.json.

    Stock lives here, in memory, and is never written back to the seed file: a
    demo that mutates its own fixtures is a demo you can only run once.
    """

    def __init__(self, seed_path: str | Path = DEFAULT_SEED) -> None:
        raw = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        self.merchants: dict[str, Merchant] = {
            m["id"]: Merchant(
                id=m["id"],
                name=m["name"],
                city=m["city"],
                serviceable_pincodes=tuple(m["serviceable_pincodes"]),
                return_days=int(m["return_days"]),
            )
            for m in raw["merchants"]
        }
        self.products: dict[str, Product] = {}
        self._stock: dict[str, int] = {}
        self._lock = threading.RLock()
        for item in raw["products"]:
            product = Product(
                sku=item["sku"],
                merchant_id=item["merchant_id"],
                merchant_name=item["merchant_name"],
                name=item["name"],
                category=item["category"],
                # Rupees in the seed file for human readability; integer paise
                # everywhere downstream. Converted exactly once, here.
                price=inr(item["price_inr"]),
                sizes=tuple(item["sizes"]),
                serviceable_pincodes=tuple(item["serviceable_pincodes"]),
                return_days=int(item["return_days"]),
            )
            self.products[product.sku] = product
            self._stock[product.sku] = int(item["stock"])

    # -- reads --------------------------------------------------------------

    def get(self, sku: str) -> Product:
        try:
            return self.products[sku]
        except KeyError:
            raise ProductNotFound(
                f"no product with sku {sku!r} exists in this catalogue", sku=sku
            ) from None

    def stock(self, sku: str) -> int:
        with self._lock:
            return self._stock.get(sku, 0)

    def search(
        self,
        query: str = "",
        *,
        max_price: int | None = None,
        min_price: int | None = None,
        category: str | None = None,
        merchant_id: str | None = None,
        size: str | None = None,
        pincode: str | None = None,
        in_stock_only: bool = True,
        limit: int = 20,
    ) -> list[Product]:
        """Substring match on name/category/sku, then filters.

        Deliberately dumb: a keyword match, not a semantic one. Search relevance
        is a place where a model would genuinely help, and it is the one place in
        the merchant where using one would be safe — but ranking shoes is not what
        this project is about, and a deterministic catalogue keeps the demo
        reproducible. See LIMITATIONS.md.

        Results are sorted by price then SKU, so the same query always returns the
        same list in the same order.
        """
        terms = [t for t in query.lower().split() if t]
        found: list[Product] = []
        for product in self.products.values():
            haystack = f"{product.name} {product.category} {product.sku}".lower()
            if terms and not all(t in haystack for t in terms):
                continue
            if category and product.category != category:
                continue
            if merchant_id and product.merchant_id != merchant_id:
                continue
            if max_price is not None and product.price > max_price:
                continue
            if min_price is not None and product.price < min_price:
                continue
            if size and product.sizes and size not in product.sizes:
                continue
            if pincode and pincode not in product.serviceable_pincodes:
                continue
            if in_stock_only and self.stock(product.sku) <= 0:
                continue
            found.append(product)
        found.sort(key=lambda p: (p.price, p.sku))
        return found[:limit]

    def serviceable_merchants(self, pincode: str) -> list[Merchant]:
        return [m for m in self.merchants.values() if pincode in m.serviceable_pincodes]

    # -- writes -------------------------------------------------------------

    def decrement(self, sku: str, qty: int) -> int:
        """Take ``qty`` off the shelf. Raises if there is not enough.

        For paths that run **before** money moves, where refusing is the correct
        answer. After a capture use :meth:`take`, which cannot raise.
        """
        with self._lock:
            available = self._stock.get(sku, 0)
            if available < qty:
                raise OutOfStock(
                    f"{sku} has {available} left, needed {qty}", sku=sku, available=available
                )
            self._stock[sku] = available - qty
            return self._stock[sku]

    def take(self, sku: str, qty: int) -> tuple[int, int]:
        """Take up to ``qty`` off the shelf. Returns ``(remaining, shortfall)``.

        **This never raises, and that is the whole point.** It is called only
        after a payment has captured, and by then the money has already moved.
        Raising there would leave the buyer charged, the merchant's code in a
        traceback, and the agent holding no receipt — the worst of all possible
        orderings, and a real crash this replaced.

        Stock is clamped at zero rather than going negative, and the shortfall is
        returned so the caller can record it loudly. An oversell is a
        *fulfilment* problem — a backorder or a refund — not a payment problem.
        The payment was authorised, verified and captured; that part was correct.

        Concurrency: several checkouts can each pass the pre-payment re-check
        while stock is still positive, then all capture. That window is real and
        cannot be closed without holding a lock across the rail round trip, which
        would be worse. So it is detected and reported instead of pretended away.
        """
        with self._lock:
            available = self._stock.get(sku, 0)
            taken = min(available, max(0, qty))
            self._stock[sku] = available - taken
            return self._stock[sku], qty - taken

    def set_stock(self, sku: str, quantity: int) -> None:
        """Set stock directly.

        Used by the demo and the tests to simulate another buyer taking the last
        unit between checkout and payment. That is a *world event*, an input to
        the scenario — not a way of faking an outcome. What happens next is
        decided entirely by :meth:`CheckoutStore.recheck`.
        """
        with self._lock:
            self._stock[sku] = max(0, quantity)


# ---------------------------------------------------------------------------
# Carts and checkouts
# ---------------------------------------------------------------------------


@dataclass
class CheckoutRecord:
    checkout_id: str
    cart: Cart
    status: str = "open"  # open | confirmed | paid | declined | expired
    checkout_jws: str | None = None
    open_checkout_mandate_hash: str | None = None
    created_ts: str = field(default_factory=lambda: utcnow().isoformat())
    stock_committed: bool = False
    #: Which payment mandate settled this checkout. Set with `stock_committed`,
    #: and used to explain a `checkout.already_settled` refusal — "paid by pm_x"
    #: is actionable in a way that "already paid" is not.
    settled_payment_mandate_id: str | None = None


@dataclass(frozen=True)
class StockCommit:
    """What taking a captured checkout's stock off the shelf actually did.

    ``shortfall`` is non-empty only when concurrent checkouts oversold a SKU. It
    is carried rather than raised because the money has already moved by the time
    this is computed — see :meth:`Catalog.take`.
    """

    remaining: dict[str, int]
    shortfall: dict[str, int]
    already_committed: bool = False

    @property
    def oversold(self) -> bool:
        return bool(self.shortfall)


class CheckoutStore:
    """Carts and checkouts in memory, with the stock/price re-check.

    In-memory rather than SQLite because carts are ephemeral and the durable
    things that matter — the audit chain, the spend ledger, the idempotency store
    — are already on disk. A crash losing an unconfirmed cart is not a
    correctness problem; a crash losing a receipt would be.
    """

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._carts: dict[str, Cart] = {}
        self._checkouts: dict[str, CheckoutRecord] = {}
        self._lock = threading.RLock()

    # -- assembly -----------------------------------------------------------

    def assemble_cart(self, items: Sequence[dict[str, Any]], *, ship_to_pincode: str) -> Cart:
        """Build a priced, single-merchant cart. Validates every SKU exists.

        Prices are stamped in at this moment. That is what makes a later price
        change detectable rather than invisible.
        """
        if not items:
            raise CatalogError("a cart needs at least one item")

        lines: list[CartItem] = []
        merchant_ids: set[str] = set()
        for raw in items:
            sku = str(raw.get("sku") or raw.get("id") or "")
            qty = int(raw.get("qty", 1))
            if qty <= 0:
                raise CatalogError(f"quantity for {sku} must be positive", sku=sku, qty=qty)
            product = self.catalog.get(sku)  # raises ProductNotFound
            available = self.catalog.stock(sku)
            if available < qty:
                raise OutOfStock(
                    f"{product.name} has {available} left, {qty} requested",
                    sku=sku,
                    available=available,
                    requested=qty,
                )
            if ship_to_pincode not in product.serviceable_pincodes:
                raise NotServiceable(
                    f"{product.merchant_name} does not deliver to {ship_to_pincode}",
                    sku=sku,
                    pincode=ship_to_pincode,
                    merchant_id=product.merchant_id,
                )
            merchant_ids.add(product.merchant_id)
            lines.append(
                CartItem(
                    sku=product.sku,
                    name=product.name,
                    qty=qty,
                    unit_price=product.price,
                    line_total=product.price * qty,
                )
            )

        if len(merchant_ids) > 1:
            raise MixedMerchantCart(
                "a cart must come from a single merchant, because one Payment "
                "Mandate names one payee",
                merchants=sorted(merchant_ids),
            )

        merchant_id = merchant_ids.pop()
        merchant = self.catalog.merchants[merchant_id]
        cart = Cart(
            cart_id=new_id("cart"),
            merchant_id=merchant_id,
            merchant_name=merchant.name,
            items=lines,
            total=sum(line.line_total for line in lines),
            ship_to_pincode=ship_to_pincode,
        )
        with self._lock:
            self._carts[cart.cart_id] = cart
        return cart

    def cart(self, cart_id: str) -> Cart:
        with self._lock:
            try:
                return self._carts[cart_id]
            except KeyError:
                raise CatalogError(f"no cart {cart_id!r}", cart_id=cart_id) from None

    # -- checkouts ----------------------------------------------------------

    def open_checkout(self, cart: Cart) -> CheckoutRecord:
        record = CheckoutRecord(checkout_id=new_id("chk"), cart=cart)
        with self._lock:
            self._checkouts[record.checkout_id] = record
        return record

    def checkout(self, checkout_id: str) -> CheckoutRecord:
        with self._lock:
            try:
                return self._checkouts[checkout_id]
            except KeyError:
                raise CatalogError(
                    f"no checkout {checkout_id!r}", checkout_id=checkout_id
                ) from None

    def checkouts(self) -> Iterable[CheckoutRecord]:
        with self._lock:
            return list(self._checkouts.values())

    def mark(self, checkout_id: str, status: str) -> CheckoutRecord:
        record = self.checkout(checkout_id)
        record.status = status
        return record

    # -- the guard ----------------------------------------------------------

    def recheck(self, cart: Cart) -> tuple[bool, str]:
        """Is this cart still buyable, at this price, right now?

        Called immediately before every payment attempt — including every recovery
        retry, because a retry can be minutes after the cart was signed.

        Returns ``(ok, reason)`` rather than raising: the caller is a recovery
        loop that needs to record the reason in an audit row and produce a signed
        receipt, not unwind a stack.
        """
        for line in cart.items:
            try:
                product = self.catalog.get(line.sku)
            except ProductNotFound:
                return False, f"{line.sku} is no longer in the catalogue"
            available = self.catalog.stock(line.sku)
            if available < line.qty:
                return (
                    False,
                    f"{product.name} ({line.sku}) is down to {available} in stock; "
                    f"the cart needs {line.qty}",
                )
            if product.price != line.unit_price:
                return (
                    False,
                    f"{product.name} ({line.sku}) is now "
                    f"₹{paise_to_inr_str(product.price)}, not the "
                    f"₹{paise_to_inr_str(line.unit_price)} the checkout was signed at",
                )
        return True, "every line is in stock at the signed price"

    def commit_stock(
        self, checkout_id: str, *, payment_mandate_id: str | None = None
    ) -> StockCommit:
        """Decrement stock for a captured checkout. Idempotent, and never raises.

        Guarded by ``stock_committed`` so a duplicate receipt — which is a normal
        outcome of the idempotency store — cannot take the same units off the
        shelf twice.

        Uses :meth:`Catalog.take` rather than :meth:`Catalog.decrement`: this runs
        *after* the money moved, so refusing is no longer an available answer. Any
        shortfall is reported for the caller to audit.
        """
        record = self.checkout(checkout_id)
        with self._lock:
            if record.stock_committed:
                return StockCommit(remaining={}, shortfall={}, already_committed=True)
            remaining: dict[str, int] = {}
            shortfall: dict[str, int] = {}
            for line in record.cart.items:
                left, short = self.catalog.take(line.sku, line.qty)
                remaining[line.sku] = left
                if short:
                    shortfall[line.sku] = short
            record.stock_committed = True
            record.status = "paid"
            record.settled_payment_mandate_id = payment_mandate_id
            return StockCommit(remaining=remaining, shortfall=shortfall)
