"""Tests for fail-closed amount extraction.

The property under test throughout: when the amount cannot be determined, the
result is ``amount=None``. Never ``0.00``. A default of zero would wave every
spend past every cap.
"""

import pytest

from paypal_guard.actions import ACTION_REGISTRY
from paypal_guard.amounts import AmountResult, extract_amount


def order_payload(value, currency="USD", units=1):
    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {"amount": {"currency_code": currency, "value": value}}
            for _ in range(units)
        ],
    }


def test_order_create_totals_a_single_unit():
    result = extract_amount(ACTION_REGISTRY["paypal_order_create"], order_payload("40.00"))
    assert result.amount == 40.00
    assert result.currency == "USD"
    assert result.known is True


def test_order_create_sums_multiple_purchase_units():
    payload = {
        "purchase_units": [
            {"amount": {"currency_code": "USD", "value": "10.25"}},
            {"amount": {"currency_code": "USD", "value": "4.75"}},
        ]
    }
    assert extract_amount(ACTION_REGISTRY["paypal_order_create"], payload).amount == 15.00


def test_order_create_refuses_mixed_currencies():
    payload = {
        "purchase_units": [
            {"amount": {"currency_code": "USD", "value": "10.00"}},
            {"amount": {"currency_code": "EUR", "value": "10.00"}},
        ]
    }
    result = extract_amount(ACTION_REGISTRY["paypal_order_create"], payload)
    assert result.amount is None
    assert "mixed currencies" in result.error


def test_invoice_amount_is_read():
    payload = {"amount": {"currency_code": "USD", "value": "129.99"}}
    result = extract_amount(ACTION_REGISTRY["paypal_invoice_create"], payload)
    assert (result.amount, result.currency) == (129.99, "USD")


def test_read_operations_cost_nothing():
    result = extract_amount(ACTION_REGISTRY["paypal_order_get"], {"id": "8XU83804L01907047"})
    assert result.amount == 0.0
    assert result.known is True


def test_capture_has_no_amount_in_its_payload():
    """orders.capture carries only an id, so it must report unknown."""
    result = extract_amount(ACTION_REGISTRY["paypal_order_capture"], {"id": "8XU83804L01907047"})
    assert result.amount is None
    assert result.known is False
    assert "resolved from PayPal" in result.error


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"purchase_units": []},
        {"purchase_units": "not-a-list"},
        {"purchase_units": [{"amount": {"currency_code": "USD"}}]},
        {"purchase_units": [{"amount": {"value": "10.00"}}]},
        {"purchase_units": [{"amount": {"currency_code": "USD", "value": "abc"}}]},
        {"purchase_units": [{"amount": {"currency_code": "USD", "value": "-5.00"}}]},
        {"purchase_units": [{"no_amount": {}}]},
    ],
)
def test_malformed_order_payloads_fail_closed(payload):
    result = extract_amount(ACTION_REGISTRY["paypal_order_create"], payload)
    assert result.amount is None
    assert result.error


def test_negative_invoice_amount_fails_closed():
    payload = {"amount": {"currency_code": "USD", "value": "-100.00"}}
    assert extract_amount(ACTION_REGISTRY["paypal_invoice_create"], payload).amount is None


def test_string_and_numeric_values_agree():
    from_string = extract_amount(
        ACTION_REGISTRY["paypal_invoice_create"],
        {"amount": {"currency_code": "USD", "value": "42.50"}},
    )
    from_number = extract_amount(
        ACTION_REGISTRY["paypal_invoice_create"],
        {"amount": {"currency_code": "USD", "value": 42.5}},
    )
    assert from_string.amount == from_number.amount == 42.50


def test_amount_result_renders_unknown_without_implying_zero():
    result = AmountResult(None, None, "paypal_order_capture", "no total")
    assert "unknown" in str(result)
    assert "0.00" not in str(result)
