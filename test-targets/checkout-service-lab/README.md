# Checkout Service Lab

A small, deterministic checkout domain used for workflow validation.

## Public contract

### Pricing

`calculate_order()` must follow these rules:

1. Each line-item quantity is a positive integer. Invalid quantities raise `ValueError`.
2. Unit prices are non-negative decimal values.
3. Supported customer tiers are `standard`, `silver`, and `gold`.
4. Tier discounts are 0%, 5%, and 10% respectively.
5. A coupon percentage must be between 0 and 50 inclusive; values outside the range raise `ValueError`.
6. The coupon is applied after the tier discount.
7. Standard shipping costs `5.00`.
8. Shipping is free only when the merchandise total after all discounts is at least `50.00`.
9. Every monetary result is rounded to two decimal places using `ROUND_HALF_UP`.

### Inventory

`can_reserve(available, requested)` returns `True` when the positive requested quantity can be fully covered by available stock. Exact depletion is allowed. Negative availability and non-positive requests raise `ValueError`.

## Development

```bash
python -m pip install -e .
python -m pytest
```
