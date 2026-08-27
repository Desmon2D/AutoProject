from decimal import Decimal

import pytest

from checkout_service.pricing import LineItem, calculate_order


def test_regular_order_below_shipping_threshold():
    result = calculate_order([LineItem("book", Decimal("10.00"), 2)])

    assert result.subtotal == Decimal("20.00")
    assert result.shipping == Decimal("5.00")
    assert result.total == Decimal("25.00")


def test_gold_discount_for_large_order():
    result = calculate_order(
        [LineItem("monitor", Decimal("100.00"), 1)],
        customer_tier="gold",
    )

    assert result.tier_discount == Decimal("10.00")
    assert result.shipping == Decimal("0.00")
    assert result.total == Decimal("90.00")


def test_coupon_is_applied_after_tier_discount():
    result = calculate_order(
        [LineItem("cable", Decimal("40.00"), 1)],
        customer_tier="silver",
        coupon_percent=Decimal("25"),
    )

    assert result.tier_discount == Decimal("2.00")
    assert result.coupon_discount == Decimal("9.50")
    assert result.total == Decimal("33.50")


def test_unknown_tier_is_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        calculate_order([], customer_tier="platinum")
