"""Tests for the closed vocabulary of PayPal operations."""

import pytest

from paypal_guard.actions import (
    ACTION_REGISTRY,
    CLOSED_VOCABULARY,
    MONEY_ACTIONS,
    TOOL_TO_ACTION,
    is_registered,
    resolve,
)


def test_verb_names_are_unique():
    assert len(CLOSED_VOCABULARY) == len(ACTION_REGISTRY)


def test_tool_paths_are_unique():
    assert len(TOOL_TO_ACTION) == len(set(TOOL_TO_ACTION.values()))


@pytest.mark.parametrize("verb", CLOSED_VOCABULARY)
def test_every_verb_resolves_to_itself(verb):
    assert resolve(verb) is not None
    assert resolve(verb).name == verb


@pytest.mark.parametrize(
    "spelling",
    [
        "orders.capture",
        "orders_capture",
        "ordersCapture",
        "capture_order",
        "captureOrder",
        "capture-order",
        "orders/capture",
    ],
)
def test_tool_name_spellings_all_resolve_to_the_same_verb(spelling):
    """An upstream rename must not silently change a permission."""
    assert resolve(spelling) is not None
    assert resolve(spelling).name == "paypal_order_capture"


def test_unregistered_verbs_resolve_to_none():
    for invented in (
        "paypal_refund_full",
        "internal_transfer",
        "spawn_subagent_v2",
        "orders.capture.override",
        "",
    ):
        assert resolve(invented) is None
        assert is_registered(invented) is False


def test_registry_cannot_be_extended_by_a_caller():
    """The vocabulary is closed, so an unknown verb is a hard refusal."""
    assert "paypal_refund_full" not in ACTION_REGISTRY


def test_money_actions_declare_an_http_method():
    for action in MONEY_ACTIONS:
        assert action.money_moving is True
        assert action.method in {"POST", "PATCH", "PUT"}


def test_capture_is_a_money_action():
    assert ACTION_REGISTRY["paypal_order_capture"].money_moving is True


def test_reads_are_not_money_moving():
    for name in ("paypal_order_get", "paypal_invoice_list", "paypal_dispute_get"):
        assert ACTION_REGISTRY[name].money_moving is False
