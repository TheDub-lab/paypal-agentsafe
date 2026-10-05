"""Tests for the gate.

The load-bearing property, asserted repeatedly: the transport is reached on
``allowed`` and on nothing else. Everything else in this file exists to make
that property hard to break by accident.
"""

import pytest

from paypal_guard import (
    AmountResult,
    PayPalGuard,
    build_protocol,
    compile_policy,
)
from paypal_guard.policy import build_from_file

MERCHANT = "M1"


class Recorder:
    """A transport that records instead of charging."""

    def __init__(self):
        self.calls = []

    def __call__(self, action, target, params):
        self.calls.append((action.name, target, dict(params)))
        return {"id": params.get("id"), "status": "COMPLETED"}


def policy_doc(**overrides):
    doc = {
        "agent_id": "agent",
        "user_id": f"merchant:{MERCHANT}",
        "budget_limit": 500.0,
        "approval_threshold_cost": 100.0,
        "allowed_action_types": [
            "paypal_order_get",
            "paypal_order_create",
            "paypal_order_capture",
        ],
        "allow": [
            {
                "action": "paypal_order_get",
                "targets": [f"paypal/merchant/{MERCHANT}/order/*"],
                "match": "glob",
                "methods": ["GET"],
                "params": {"required": ["id"], "additional_properties": False,
                           "properties": {"id": {"type": "string"}}},
            },
            {
                "action": "paypal_order_create",
                "targets": [f"paypal/merchant/{MERCHANT}/order/*"],
                "match": "glob",
                "methods": ["POST"],
                "max_cost": 75.0,
                "params": {"required": ["intent", "purchase_units"],
                           "additional_properties": False,
                           "properties": {"intent": {"type": "string"},
                                          "purchase_units": {"type": "array"}}},
            },
            {
                "action": "paypal_order_capture",
                "targets": [f"paypal/merchant/{MERCHANT}/order/*"],
                "match": "glob",
                "methods": ["POST"],
                "max_cost": 300.0,
                "requires_approval": True,
                "params": {"required": ["id"], "additional_properties": False,
                           "properties": {"id": {"type": "string"}}},
            },
        ],
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def guard():
    recorder = Recorder()
    g = PayPalGuard(build_protocol(compile_policy(policy_doc())), transport=recorder)
    g.recorder = recorder
    return g


def target(order_id, merchant=MERCHANT):
    return f"paypal/merchant/{merchant}/order/{order_id}"


def order(amount):
    return {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": "USD", "value": amount}}],
    }


def resolver_for(totals):
    def resolve_amount(action, params):
        total = totals.get(params.get("id"))
        return AmountResult(total, "USD", "orders.get") if total is not None else None
    return resolve_amount


# ---------------------------------------------------------------------------
# The load-bearing property
# ---------------------------------------------------------------------------

def test_allowed_action_reaches_the_transport(guard):
    decision = guard.call("paypal_order_get", target("8XU1"), {"id": "8XU1"})
    assert decision.allowed
    assert len(guard.recorder.calls) == 1
    assert decision.payload == {"id": "8XU1", "status": "COMPLETED"}


def test_blocked_action_never_reaches_the_transport(guard):
    decision = guard.call("paypal_order_get", "paypal/merchant/OTHER/order/8XU1",
                          {"id": "8XU1"})
    assert decision.blocked
    assert guard.recorder.calls == []


def test_pending_action_never_reaches_the_transport(guard):
    guard.set_amount_resolver(resolver_for({"8XU1": 240.0}))
    decision = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert decision.needs_approval
    assert guard.recorder.calls == []


# ---------------------------------------------------------------------------
# Closed vocabulary
# ---------------------------------------------------------------------------

def test_invented_verb_is_refused_before_any_rule(guard):
    decision = guard.call("paypal_refund_full", target("8XU1"), {"id": "8XU1"})
    assert decision.outcome == "blocked_unknown_action"
    assert "closed" in decision.reason
    assert guard.recorder.calls == []


def test_invented_verb_is_still_audited(guard):
    guard.call("paypal_refund_full", target("8XU1"), {"id": "8XU1"})
    refusals = guard.protocol.audit.query(event_types=["action_refused"])
    assert len(refusals) == 1
    assert refusals[0]["data"]["outcome"] == "blocked_unknown_action"


# ---------------------------------------------------------------------------
# Fail-closed pricing
# ---------------------------------------------------------------------------

def test_unpriceable_money_action_is_refused(guard):
    decision = guard.call("paypal_order_capture", target("NOPRICE"), {"id": "NOPRICE"})
    assert decision.outcome == "blocked_unknown_amount"
    assert guard.recorder.calls == []


def test_amount_resolver_supplies_the_price(guard):
    guard.set_amount_resolver(resolver_for({"8XU1": 50.0}))
    decision = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert decision.amount == 50.0
    assert decision.amount_source == "orders.get"


def test_resolver_cannot_launder_a_missing_amount(guard):
    guard.set_amount_resolver(lambda action, params: AmountResult(None, None, "x", "nope"))
    decision = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert decision.outcome == "blocked_unknown_amount"


def test_order_create_without_a_parseable_amount_is_refused(guard):
    """Schema-valid, but there is no figure to cap against."""
    decision = guard.call("paypal_order_create", target("new/1"),
                          {"intent": "CAPTURE", "purchase_units": []})
    assert decision.outcome == "blocked_unknown_amount"
    assert guard.recorder.calls == []


def test_schema_violation_is_caught_before_pricing(guard):
    """A missing required param is a scope failure, not a pricing one."""
    decision = guard.call("paypal_order_create", target("new/1"), {"intent": "CAPTURE"})
    assert decision.outcome == "blocked_scope"
    assert "required param" in decision.reason


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def test_another_merchant_is_out_of_scope(guard):
    decision = guard.call("paypal_order_get", target("8XU1", "GLOBEX"), {"id": "8XU1"})
    assert decision.outcome == "blocked_scope"
    assert "allowlist" in decision.reason


def test_verb_outside_the_policy_vocabulary_is_denied(guard):
    decision = guard.call("paypal_invoice_list", f"paypal/merchant/{MERCHANT}/invoice/*", {})
    assert decision.blocked


def test_params_are_validated_against_the_schema(guard):
    decision = guard.call("paypal_order_get", target("8XU1"), {})
    assert decision.blocked
    assert "required param" in decision.reason


def test_unexpected_param_is_rejected(guard):
    decision = guard.call("paypal_order_get", target("8XU1"),
                          {"id": "8XU1", "confirm": True})
    assert decision.blocked
    assert "not permitted" in decision.reason


def test_forbidden_targets_are_checked():
    doc = policy_doc()
    doc["allow"][2]["forbidden_targets"] = ["*unverified*"]
    doc["allow"][2]["forbid_match"] = "glob"
    g = PayPalGuard(build_protocol(compile_policy(doc)))
    decision = g.call("paypal_order_capture", target("unverified-1"), {"id": "unverified-1"})
    assert decision.blocked
    assert "forbidden" in decision.reason


# ---------------------------------------------------------------------------
# Caps and budget
# ---------------------------------------------------------------------------

def test_per_action_cap_is_enforced(guard):
    decision = guard.call("paypal_order_create", target("new/1"), order("900.00"))
    assert decision.blocked
    assert "per-rule cap" in decision.reason
    assert guard.recorder.calls == []


def test_session_budget_is_enforced(guard):
    for i in range(20):
        decision = guard.call("paypal_order_create", target(f"new/{i}"), order("75.00"))
        if decision.blocked:
            assert decision.outcome == "blocked_budget"
            assert "budget limit" in decision.reason
            break
    else:
        pytest.fail("budget was never enforced")


def test_budget_is_not_charged_by_a_blocked_action(guard):
    before = guard.protocol.monitor.total_cost
    guard.call("paypal_order_create", target("new/1"), order("900.00"))
    assert guard.protocol.monitor.total_cost == before


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------

def test_approval_then_retry_executes(guard):
    guard.set_amount_resolver(resolver_for({"8XU1": 240.0}))
    first = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert first.needs_approval

    guard.approve(first.approval_token, "ops", "checked the invoice")
    retry = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert retry.allowed
    assert len(guard.recorder.calls) == 1


def test_denial_keeps_the_transport_shut(guard):
    guard.set_amount_resolver(resolver_for({"8XU1": 240.0}))
    first = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    guard.deny(first.approval_token, "ops", "wrong customer")
    retry = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert retry.needs_approval
    assert guard.recorder.calls == []


def test_approval_does_not_double_charge_the_budget(guard):
    guard.set_amount_resolver(resolver_for({"8XU1": 240.0}))
    first = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    guard.approve(first.approval_token, "ops")
    guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert guard.protocol.monitor.total_cost == 240.0


def test_approval_whitelists_only_the_same_intent(guard):
    guard.set_amount_resolver(resolver_for({"8XU1": 240.0, "8XU2": 240.0}))
    first = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    guard.approve(first.approval_token, "ops")
    other = guard.call("paypal_order_capture", target("8XU2"), {"id": "8XU2"})
    assert other.needs_approval, "approval must not transfer to a different order"


def test_urgent_actions_are_held_for_a_human(guard):
    decision = guard.call("paypal_order_create", target("new/1"), order("1.00"),
                          urgency="critical")
    assert decision.needs_approval


def test_pending_queue_lists_the_amount(guard):
    guard.set_amount_resolver(resolver_for({"8XU1": 240.0}))
    guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    queue = guard.pending()
    assert queue[0]["estimated_cost"] == 240.0


# ---------------------------------------------------------------------------
# Kill switch and revocation
# ---------------------------------------------------------------------------

def test_kill_switch_freezes_everything(guard):
    guard.kill("fraud alert")
    decision = guard.call("paypal_order_get", target("8XU1"), {"id": "8XU1"})
    assert decision.outcome == "blocked_killswitch"
    assert guard.recorder.calls == []


def test_kill_switch_overrides_an_approval(guard):
    guard.set_amount_resolver(resolver_for({"8XU1": 240.0}))
    first = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    guard.approve(first.approval_token, "ops")
    guard.kill("fraud alert")
    retry = guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert retry.outcome == "blocked_killswitch"


def test_revocation_survives_unfreezing(guard):
    guard.kill("pause")
    guard.revoke("account closed")
    guard.unfreeze()
    decision = guard.call("paypal_order_get", target("8XU1"), {"id": "8XU1"})
    assert decision.blocked
    assert guard.recorder.calls == []


def test_frozen_guard_does_not_call_the_amount_resolver(guard):
    """A freeze must stop PayPal lookups, not just the money movement."""
    lookups = []

    def spy(action, params):
        lookups.append(params.get("id"))
        return AmountResult(240.0, "USD", "orders.get")

    guard.set_amount_resolver(spy)
    guard.kill("fraud alert")
    guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert lookups == [], "resolver ran after the kill switch was engaged"


def test_revoked_guard_does_not_call_the_amount_resolver(guard):
    lookups = []
    guard.set_amount_resolver(lambda action, params: lookups.append(1))
    guard.revoke("account closed")
    guard.call("paypal_order_capture", target("8XU1"), {"id": "8XU1"})
    assert lookups == []


def test_out_of_scope_action_does_not_call_the_amount_resolver(guard):
    """Scope is settled before any priced work is done on the agent's behalf."""
    lookups = []
    guard.set_amount_resolver(lambda action, params: lookups.append(params))
    guard.call("paypal_order_capture", target("8XU1", "OTHER"), {"id": "8XU1"})
    assert lookups == []


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_audit_chain_is_intact_after_normal_operation(guard):
    guard.call("paypal_order_get", target("8XU1"), {"id": "8XU1"})
    guard.call("paypal_refund_full", target("8XU1"), {"id": "8XU1"})
    guard.kill("fraud alert")
    assert guard.protocol.audit.verify_integrity() == []


def test_editing_an_audited_amount_breaks_the_chain(guard):
    guard.call("paypal_order_create", target("new/1"), order("40.00"))
    entries = guard.protocol.audit.query(event_types=["action_allowed"])
    entries[0]["data"]["cost"] = 0.01
    assert guard.protocol.audit.verify_integrity() != []


def test_credentials_are_redacted_before_audit():
    """A sloppy agent cannot write a secret into a tamper-evident log."""
    doc = policy_doc()
    doc["allow"][0]["params"]["additional_properties"] = True
    g = PayPalGuard(build_protocol(compile_policy(doc)))
    decision = g.call("paypal_order_get", target("8XU1"),
                      {"id": "8XU1", "client_secret": "super-secret"})
    assert decision.allowed, "precondition: the action must reach the audit log"
    blob = str(g.protocol.audit.query())
    assert "super-secret" not in blob
    assert "[redacted]" in blob


def test_status_reports_a_clean_chain(guard):
    guard.call("paypal_order_get", target("8XU1"), {"id": "8XU1"})
    status = guard.status()
    assert status["audit_broken_links"] == []
    assert status["monitor"]["action_count"] > 0


# ---------------------------------------------------------------------------
# Shipped policy
# ---------------------------------------------------------------------------

def test_shipped_autopilot_policy_gates_a_capture():
    _, protocol = build_from_file("policies/autopilot.json")
    g = PayPalGuard(protocol, transport=Recorder())
    g.set_amount_resolver(resolver_for({"8XU83804L01907047": 240.0}))
    decision = g.call("paypal_order_capture",
                      "paypal/merchant/ACME-MERCH-4417/order/8XU83804L01907047",
                      {"id": "8XU83804L01907047"})
    assert decision.needs_approval
    assert g.transport.calls == []


def test_guard_without_a_transport_still_decides():
    _, protocol = build_from_file("policy.example.json")
    g = PayPalGuard(protocol)
    decision = g.call("paypal_order_get",
                      "paypal/merchant/ACME-MERCH-4417/order/8XU83804L01907047",
                      {"id": "8XU83804L01907047"})
    assert decision.allowed
    assert decision.payload is None
