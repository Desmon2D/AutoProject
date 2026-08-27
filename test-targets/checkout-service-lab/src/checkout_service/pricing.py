from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

CENT = Decimal("0.01")
SHIPPING_THRESHOLD = Decimal("50.00")
STANDARD_SHIPPING = Decimal("5.00")
TIER_DISCOUNTS = {
    "standard": Decimal("0.00"),
    "silver": Decimal("0.05"),
    "gold": Decimal("0.10"),
}


@dataclass(frozen=True)
class LineItem:
    sku: str
    unit_price: Decimal
    quantity: int


@dataclass(frozen=True)
class OrderTotal:
    subtotal: Decimal
    tier_discount: Decimal
    coupon_discount: Decimal
    shipping: Decimal
    total: Decimal


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_order(
    items: Iterable[LineItem],
    *,
    customer_tier: str = "standard",
    coupon_percent: Decimal = Decimal("0"),
) -> OrderTotal:
    lines = list(items)
    if customer_tier not in TIER_DISCOUNTS:
        raise ValueError(f"unsupported customer tier: {customer_tier}")

    for item in lines:
        if item.unit_price < 0:
            raise ValueError("unit price cannot be negative")

    subtotal = _money(sum((item.unit_price * item.quantity for item in lines), Decimal("0")))
    tier_discount = _money(subtotal * TIER_DISCOUNTS[customer_tier])
    after_tier = subtotal - tier_discount
    coupon_discount = _money(after_tier * Decimal(coupon_percent) / Decimal("100"))
    merchandise_total = _money(after_tier - coupon_discount)

    shipping = Decimal("0.00") if subtotal >= SHIPPING_THRESHOLD else STANDARD_SHIPPING
    total = _money(merchandise_total + shipping)

    return OrderTotal(
        subtotal=subtotal,
        tier_discount=tier_discount,
        coupon_discount=coupon_discount,
        shipping=shipping,
        total=total,
    )
