import pytest

from checkout_service.inventory import can_reserve


def test_reservation_with_surplus_stock():
    assert can_reserve(available=10, requested=3) is True


def test_reservation_over_available_stock():
    assert can_reserve(available=3, requested=4) is False


def test_non_positive_request_is_rejected():
    with pytest.raises(ValueError):
        can_reserve(available=3, requested=0)
