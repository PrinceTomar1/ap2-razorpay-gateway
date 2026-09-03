"""The Merchant role: catalogue, carts, checkout, and the order of operations."""

from __future__ import annotations

import pytest

from ap2_min.models import inr
from gateway.audit import Event
from gateway.bootstrap import Gateway
from merchant.checkout import (
    Catalog,
    CheckoutStore,
    MixedMerchantCart,
    NotServiceable,
    OutOfStock,
    ProductNotFound,
)

# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog() -> Catalog:
    return Catalog()


@pytest.fixture
def store(catalog: Catalog) -> CheckoutStore:
    return CheckoutStore(catalog)


def test_the_seed_has_sixty_skus_across_three_merchants(catalog: Catalog) -> None:
    assert len(catalog.products) == 60
    assert len(catalog.merchants) == 3
    by_merchant: dict[str, int] = {}
    for product in catalog.products.values():
        by_merchant[product.merchant_id] = by_merchant.get(product.merchant_id, 0) + 1
    assert by_merchant == {"m_stridefit": 20, "m_lumen": 20, "m_pixelbyte": 20}


def test_prices_are_integer_paise_not_rupees(catalog: Catalog) -> None:
    """A float rupee anywhere in the catalogue is a bug waiting for a decimal."""
    shoe = catalog.get("SF-RUN-001")
    assert shoe.price == 129900
    assert isinstance(shoe.price, int)
    assert all(isinstance(p.price, int) for p in catalog.products.values())


def test_every_merchant_serves_the_demo_pincode(catalog: Catalog) -> None:
    assert len(catalog.serviceable_merchants("560001")) == 3
    assert catalog.serviceable_merchants("999999") == []


def test_search_is_deterministic_and_price_sorted(catalog: Catalog) -> None:
    runs = [[p.sku for p in catalog.search("running", category="running_shoes")] for _ in range(5)]
    assert all(run == runs[0] for run in runs)
    prices = [catalog.get(sku).price for sku in runs[0]]
    assert prices == sorted(prices)


def test_search_filters_compose(catalog: Catalog) -> None:
    found = catalog.search(
        "running", category="running_shoes", max_price=inr(1500), size="9", pincode="560001"
    )
    assert found
    for product in found:
        assert product.category == "running_shoes"
        assert product.price <= inr(1500)
        assert "9" in product.sizes
        assert "560001" in product.serviceable_pincodes


def test_search_hides_out_of_stock_by_default(catalog: Catalog) -> None:
    catalog.set_stock("SF-RUN-001", 0)
    assert "SF-RUN-001" not in [p.sku for p in catalog.search("velocity")]
    assert "SF-RUN-001" in [p.sku for p in catalog.search("velocity", in_stock_only=False)]


def test_an_unknown_sku_raises_product_not_found(catalog: Catalog) -> None:
    with pytest.raises(ProductNotFound) as excinfo:
        catalog.get("SF-RUN-999")
    assert excinfo.value.code == "product.not_found"
    assert excinfo.value.as_dict()["sku"] == "SF-RUN-999"


def test_decrement_refuses_to_oversell(catalog: Catalog) -> None:
    catalog.set_stock("SF-RUN-001", 2)
    assert catalog.decrement("SF-RUN-001", 2) == 0
    with pytest.raises(OutOfStock):
        catalog.decrement("SF-RUN-001", 1)


# ---------------------------------------------------------------------------
# Carts
# ---------------------------------------------------------------------------


def test_a_cart_stamps_in_the_price_at_assembly(store: CheckoutStore) -> None:
    cart = store.assemble_cart([{"sku": "SF-RUN-001", "qty": 2}], ship_to_pincode="560001")
    assert cart.total == inr(2598)
    assert cart.items[0].unit_price == inr(1299)
    assert cart.merchant_id == "m_stridefit"


def test_a_cart_cannot_span_two_merchants(store: CheckoutStore) -> None:
    """One Payment Mandate names one payee, so one cart means one merchant."""
    with pytest.raises(MixedMerchantCart) as excinfo:
        store.assemble_cart(
            [{"sku": "SF-RUN-001", "qty": 1}, {"sku": "LM-KIT-002", "qty": 1}],
            ship_to_pincode="560001",
        )
    assert excinfo.value.code == "cart.mixed_merchants"


def test_a_cart_refuses_an_unserviceable_pincode(store: CheckoutStore) -> None:
    with pytest.raises(NotServiceable):
        store.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], ship_to_pincode="999999")


def test_a_cart_refuses_more_than_the_shelf_holds(store: CheckoutStore) -> None:
    with pytest.raises(OutOfStock):
        store.assemble_cart([{"sku": "SF-RUN-004", "qty": 99}], ship_to_pincode="560001")


def test_a_cart_refuses_a_nonexistent_sku(store: CheckoutStore) -> None:
    with pytest.raises(ProductNotFound):
        store.assemble_cart([{"sku": "NOPE-001", "qty": 1}], ship_to_pincode="560001")


# ---------------------------------------------------------------------------
# The stock/price re-check — failure mode 5
# ---------------------------------------------------------------------------


def test_recheck_passes_while_nothing_changes(store: CheckoutStore) -> None:
    cart = store.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], ship_to_pincode="560001")
    ok, reason = store.recheck(cart)
    assert ok
    assert "signed price" in reason


def test_recheck_catches_stock_disappearing(store: CheckoutStore, catalog: Catalog) -> None:
    cart = store.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], ship_to_pincode="560001")
    catalog.set_stock("SF-RUN-001", 0)
    ok, reason = store.recheck(cart)
    assert not ok
    assert "down to 0 in stock" in reason


def test_recheck_catches_a_price_change(store: CheckoutStore, catalog: Catalog) -> None:
    """The signed cart carries prices precisely so a change is detectable."""
    from dataclasses import replace

    cart = store.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], ship_to_pincode="560001")
    catalog.products["SF-RUN-001"] = replace(catalog.get("SF-RUN-001"), price=inr(1999))
    ok, reason = store.recheck(cart)
    assert not ok
    assert "1,999.00" in reason
    assert "1,299.00" in reason


def test_recheck_catches_a_delisted_product(store: CheckoutStore, catalog: Catalog) -> None:
    cart = store.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], ship_to_pincode="560001")
    del catalog.products["SF-RUN-001"]
    ok, reason = store.recheck(cart)
    assert not ok
    assert "no longer in the catalogue" in reason


def test_stock_is_committed_on_capture_not_on_checkout(store: CheckoutStore) -> None:
    before = store.catalog.stock("SF-RUN-001")
    cart = store.assemble_cart([{"sku": "SF-RUN-001", "qty": 2}], ship_to_pincode="560001")
    record = store.open_checkout(cart)
    assert store.catalog.stock("SF-RUN-001") == before, "checkout must not touch stock"
    store.commit_stock(record.checkout_id)
    assert store.catalog.stock("SF-RUN-001") == before - 2


def test_committing_stock_twice_takes_the_units_once(store: CheckoutStore) -> None:
    """A duplicate receipt is a normal outcome of idempotency; a double
    decrement would not be."""
    before = store.catalog.stock("SF-RUN-001")
    cart = store.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}], ship_to_pincode="560001")
    record = store.open_checkout(cart)
    store.commit_stock(record.checkout_id)
    store.commit_stock(record.checkout_id)
    assert store.catalog.stock("SF-RUN-001") == before - 1


# ---------------------------------------------------------------------------
# The service, end to end
# ---------------------------------------------------------------------------


def test_a_clean_purchase_runs_the_whole_lifecycle(wired: Gateway) -> None:
    merchant = wired.merchant
    cart = merchant.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    assert checkout["checkout_mandate_jwt"]
    assert checkout["open_checkout_mandate_template"]["vct"] == "mandate.checkout.open.1"

    confirmed = merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)
    assert confirmed["status"] == "confirmed"
    assert confirmed["checkout_receipt"]["amount"] == inr(1299)

    from ap2_min.builders import closed_payment_mandate
    from gateway.mandates import utcnow

    now = utcnow()
    mandate = closed_payment_mandate(
        payee=cart["merchant_id"],
        payee_name=cart["merchant_name"],
        amount=cart["total"],
        payment_instrument="upi",
        checkout_hash=checkout["checkout_hash"],
        open_mandate_jws=wired.open_payment_jws,
        execution_date=now,
    )
    paid = merchant.initiate_payment(
        checkout["checkout_id"], wired.agent.sign(mandate, ttl_seconds=600, now=now)
    )
    assert paid["status"] == "captured"
    assert paid["payment_receipt"]["amount"] == inr(1299)
    assert wired.audit.verify_chain().ok


def test_payment_before_checkout_confirmation_is_refused(wired: Gateway) -> None:
    """Order of operations is enforced, not assumed."""
    merchant = wired.merchant
    cart = merchant.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    response = merchant.initiate_payment(checkout["checkout_id"], "anything")
    assert response["error"] == "checkout.not_confirmed"


def test_an_over_limit_checkout_raises_the_gate_before_any_payment(wired: Gateway) -> None:
    merchant = wired.merchant
    cart = merchant.assemble_cart([{"sku": "SF-RUN-004", "qty": 1}])["cart"]
    assert cart["total"] == inr(4999)
    checkout = merchant.create_checkout(cart["cart_id"])
    response = merchant.complete_checkout(checkout["checkout_id"], wired.open_checkout_jws)

    assert response["error"] == "unresolved_constraint"
    assert response["constraint"] == "checkout.amount_exceeds_standing_limit"
    assert response["hold_id"]
    assert response["approval_url"].endswith(response["hold_id"])
    assert wired.audit.rows(event=Event.CHECKOUT_UNRESOLVED)
    assert wired.trusted_surface.pending()


def test_a_checkout_mandate_from_the_wrong_role_is_refused(wired: Gateway) -> None:
    """A merchant-signed 'buyer authorisation' is not a buyer authorisation."""
    merchant = wired.merchant
    cart = merchant.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}])["cart"]
    checkout = merchant.create_checkout(cart["cart_id"])
    forged = wired.merchant_signer.sign(wired.open_checkout_contents, ttl_seconds=600)
    response = merchant.complete_checkout(checkout["checkout_id"], forged)
    assert response["error"] == "mandate.wrong_issuer"


def test_creating_a_checkout_for_a_sold_out_cart_is_refused(wired: Gateway) -> None:
    merchant = wired.merchant
    cart = merchant.assemble_cart([{"sku": "SF-RUN-001", "qty": 1}])["cart"]
    wired.catalog.set_stock("SF-RUN-001", 0)
    response = merchant.create_checkout(cart["cart_id"])
    assert response["error"] == "stock.unavailable"


def test_check_product_answers_not_found_without_signing_anything(wired: Gateway) -> None:
    response = wired.merchant.check_product("SF-RUN-999")
    assert response["error"] == "product.not_found"
    assert wired.audit.rows(event=Event.PRODUCT_NOT_FOUND)
    assert wired.audit.count(Event.CART_ASSEMBLED) == 0


def test_serviceability_lists_the_merchants_that_deliver(wired: Gateway) -> None:
    response = wired.merchant.check_serviceability("560001")
    assert response["serviceable"] is True
    assert len(response["merchants"]) == 3
    assert wired.merchant.check_serviceability("999999")["serviceable"] is False


def test_search_filters_accept_rupees_from_the_agent(wired: Gateway) -> None:
    """Agents think in rupees; everything past this boundary is paise."""
    found = wired.merchant.search_inventory("running", {"max_price_inr": 1500})
    assert found["count"] > 0
    assert all(p["price_paise"] <= inr(1500) for p in found["results"])
