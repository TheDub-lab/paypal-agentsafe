"""Tests for policy compilation.

The compile step is the deployment-time guardrail: a policy that is malformed,
grants an unregistered operation, or is broader than the operator meant is
rejected before an agent exists.
"""

import json
from pathlib import Path

import pytest

from paypal_guard.policy import PolicyError, build_protocol, compile_policy, load_policy

ROOT = Path(__file__).resolve().parent.parent


def minimal_rule(**overrides):
    rule = {
        "action": "paypal_order_get",
        "targets": ["paypal/merchant/M1/order/*"],
        "match": "glob",
        "methods": ["GET"],
        "params": {"required": ["id"], "additional_properties": False,
                   "properties": {"id": {"type": "string"}}},
    }
    rule.update(overrides)
    return rule


def document(**overrides):
    doc = {
        "agent_id": "agent",
        "user_id": "merchant:M1",
        "budget_limit": 100.0,
        "approval_threshold_cost": 50.0,
        "allow": [minimal_rule()],
    }
    doc.update(overrides)
    return doc


def test_minimal_policy_compiles():
    policy = compile_policy(document())
    assert policy.agent_id == "agent"
    assert policy.rules[0].action_type == "paypal_order_get"


def test_missing_agent_id_is_rejected():
    doc = document()
    del doc["agent_id"]
    with pytest.raises(PolicyError, match="agent_id"):
        compile_policy(doc)


def test_missing_user_id_is_rejected():
    doc = document()
    del doc["user_id"]
    with pytest.raises(PolicyError, match="user_id"):
        compile_policy(doc)


def test_empty_allow_list_is_rejected():
    with pytest.raises(PolicyError, match="non-empty 'allow'"):
        compile_policy(document(allow=[]))


def test_unregistered_verb_is_rejected():
    """A typo would otherwise become a rule that never matches."""
    with pytest.raises(PolicyError, match="not a registered PayPal operation"):
        compile_policy(document(allow=[minimal_rule(action="paypal_order_caputre")]))


def test_invented_verb_is_rejected():
    with pytest.raises(PolicyError, match="not a registered PayPal operation"):
        compile_policy(document(allow=[minimal_rule(action="paypal_refund_full")]))


def test_unregistered_verb_in_vocabulary_is_rejected():
    with pytest.raises(PolicyError, match="unregistered operations"):
        compile_policy(document(allowed_action_types=["paypal_order_get", "paypal_magic"]))


def test_rule_without_targets_is_rejected():
    with pytest.raises(PolicyError, match="no 'targets'"):
        compile_policy(document(allow=[{"action": "paypal_order_get"}]))


def test_unknown_rule_key_is_rejected():
    with pytest.raises(PolicyError, match="unknown keys"):
        compile_policy(document(allow=[minimal_rule(budget="lots")]))


def test_rule_without_method_binding_is_rejected_by_the_linter():
    """A read rule that also permits POST is not least privilege."""
    rule = minimal_rule()
    del rule["methods"]
    with pytest.raises(PolicyError, match="NO_METHOD"):
        compile_policy(document(allow=[rule]))


def test_rule_without_param_schema_is_rejected_by_the_linter():
    rule = minimal_rule()
    del rule["params"]
    with pytest.raises(PolicyError, match="NO_PARAM_SCHEMA"):
        compile_policy(document(allow=[rule]))


def test_catch_all_prefix_rule_is_rejected_by_the_linter():
    rule = minimal_rule(
        action="paypal_order_create",
        targets=["https://api-m.sandbox.paypal.com/"],
        match="prefix",
        params={"required": ["intent"], "additional_properties": False,
                "properties": {"intent": {"type": "string"}}},
    )
    with pytest.raises(PolicyError, match="CATCH_ALL_PREFIX"):
        compile_policy(document(allow=[rule]))


def test_contradictory_rule_is_rejected_by_the_linter():
    rule = minimal_rule(forbidden_targets=["paypal/merchant/M1/order/*"], forbid_match="glob")
    with pytest.raises(PolicyError, match="ALLOW_FORBID_CONFLICT"):
        compile_policy(document(allow=[rule]))


def test_blanket_allow_rule_is_rejected_by_the_linter():
    rule = minimal_rule()
    del rule["targets"]
    with pytest.raises(PolicyError, match="no 'targets'"):
        compile_policy(document(allow=[rule]))


def test_negative_max_cost_is_rejected():
    with pytest.raises(PolicyError, match="cannot be negative"):
        compile_policy(document(allow=[minimal_rule(max_cost=-1)]))


@pytest.mark.parametrize("path", ["policy.example.json", "policies/autopilot.json"])
def test_shipped_policies_compile_clean(path):
    policy = load_policy(ROOT / path)
    protocol = build_protocol(policy)
    assert protocol.verify_binding() is True


def test_load_policy_reports_a_missing_file():
    with pytest.raises(PolicyError, match="not found"):
        load_policy(ROOT / "policies" / "nope.json")


def test_load_policy_reports_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(PolicyError, match="not valid JSON"):
        load_policy(bad)


def test_policy_describe_mentions_the_agent_and_budget():
    text = compile_policy(document()).describe()
    assert "agent" in text
    assert "$100.00" in text


def test_compiled_vocabulary_is_the_union_of_granted_verbs():
    doc = document(allow=[minimal_rule(), minimal_rule(action="paypal_order_create",
                                                       max_cost=10.0)])
    policy = compile_policy(doc)
    assert set(policy.allowed_action_types) == {"paypal_order_get", "paypal_order_create"}


def test_json_round_trip(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document()), encoding="utf-8")
    assert load_policy(path).agent_id == "agent"
