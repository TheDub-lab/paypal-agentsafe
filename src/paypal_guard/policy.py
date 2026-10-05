"""
A PayPal policy document, compiled into an enforceable protocol.

A prompt says what the agent *should* do. A policy document says what it is
*allowed* to do, and this module turns the second into a live
:class:`~paypal_guard.governance.SafetyProtocol`.

The compile step fails closed, in two places:

1. **Unknown verb.** A policy that grants an action outside PayPal's closed
   vocabulary is rejected. A typo like ``paypal_order_caputre`` would otherwise
   become a rule that never matches, and the agent would be silently denied
   while the operator believes it is allowed.

2. **Scope lint.** The vendored linter statically inspects the compiled rules
   for the ways a permission is broader than it looks — an allowlist with no
   method binding, no param schema, or no per-action cost cap. Any ERROR or
   WARN blocks the build. An agent that cannot be safely scoped does not boot.

The result is a protocol object, plus the vocabulary the agent is held to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actions import CLOSED_VOCABULARY
from .governance import (
    SafetyProtocol,
    ScopeRule,
    Severity,
    lint_report,
    lint_rules,
)


class PolicyError(ValueError):
    """The policy document is invalid or too broad to enforce safely."""


@dataclass
class Policy:
    """A validated, lint-clean set of permissions for one agent and one user."""

    agent_id: str
    user_id: str
    budget_limit: float | None
    approval_threshold_cost: float
    rules: list[ScopeRule] = field(default_factory=list)
    allowed_action_types: list[str] = field(default_factory=list)
    source: str = "<inline>"

    def describe(self) -> str:
        lines = [
            f"policy     : {self.source}",
            f"agent      : {self.agent_id}",
            f"bound to   : {self.user_id}",
            "budget     : "
            + ("unlimited" if self.budget_limit is None else f"${self.budget_limit:.2f}")
            + f"   approval above: ${self.approval_threshold_cost:.2f}",
            f"vocabulary : {len(self.allowed_action_types)} PayPal operations "
            f"of {len(CLOSED_VOCABULARY)} registered",
            "permissions:",
        ]
        for rule in self.rules:
            targets = ", ".join(rule.allowed_targets or ["<any target>"])
            cap = "no cap" if rule.max_cost is None else f"cap ${rule.max_cost:.2f}"
            approval = " +approval" if rule.requires_approval else ""
            lines.append(f"  - {rule.action_type}: {targets} [{rule.match}] {cap}{approval}")
        return "\n".join(lines)


_ALLOWED_RULE_KEYS = {
    "action",
    "targets",
    "match",
    "forbidden_targets",
    "forbid_match",
    "methods",
    "params",
    "max_cost",
    "requires_approval",
}


def _compile_rule(index: int, spec: Any) -> ScopeRule:
    if not isinstance(spec, dict):
        raise PolicyError(f"allow[{index}] must be an object, got {type(spec).__name__}")

    unknown = set(spec) - _ALLOWED_RULE_KEYS
    if unknown:
        raise PolicyError(
            f"allow[{index}] has unknown keys {sorted(unknown)}; "
            f"permitted keys are {sorted(_ALLOWED_RULE_KEYS)}"
        )

    action = spec.get("action")
    if not action:
        raise PolicyError(f"allow[{index}] is missing 'action'")
    if action not in CLOSED_VOCABULARY:
        raise PolicyError(
            f"allow[{index}] grants '{action}', which is not a registered PayPal "
            f"operation. An unregistered verb can never be authorised — if you "
            f"meant something else, the rule would silently never match."
        )

    targets = spec.get("targets")
    if not targets:
        raise PolicyError(
            f"allow[{index}] ('{action}') has no 'targets'. Scope is an "
            f"allowlist: name exactly what may be touched."
        )
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        raise PolicyError(f"allow[{index}] 'targets' must be a list of strings")

    max_cost = spec.get("max_cost")
    if max_cost is not None and not isinstance(max_cost, (int, float)):
        raise PolicyError(f"allow[{index}] 'max_cost' must be a number")
    if isinstance(max_cost, (int, float)) and max_cost < 0:
        raise PolicyError(f"allow[{index}] 'max_cost' cannot be negative")

    params = spec.get("params")
    if params is not None and not isinstance(params, dict):
        raise PolicyError(f"allow[{index}] 'params' must be a JSON-schema object")

    return ScopeRule(
        action_type=action,
        allowed_targets=list(targets),
        forbidden_targets=spec.get("forbidden_targets"),
        match=spec.get("match", "glob"),
        forbid_match=spec.get("forbid_match", "token"),
        methods=spec.get("methods"),
        param_schema=params,
        max_cost=float(max_cost) if max_cost is not None else None,
        requires_approval=bool(spec.get("requires_approval", False)),
    )


def compile_policy(document: dict, source: str = "<inline>") -> Policy:
    """Validate a policy document and compile it to a :class:`Policy`.

    Raises:
        PolicyError: if the document is malformed, grants an unregistered
            operation, or compiles to a ruleset the linter rejects.
    """
    if not isinstance(document, dict):
        raise PolicyError("policy must be a JSON object")

    agent_id = document.get("agent_id")
    user_id = document.get("user_id")
    if not agent_id:
        raise PolicyError("policy is missing 'agent_id'")
    if not user_id:
        raise PolicyError("policy is missing 'user_id'")

    allow = document.get("allow")
    if not isinstance(allow, list) or not allow:
        raise PolicyError("policy must contain a non-empty 'allow' list")

    rules = [_compile_rule(i, spec) for i, spec in enumerate(allow)]

    vocabulary = document.get("allowed_action_types")
    if vocabulary is None:
        vocabulary = sorted({r.action_type for r in rules if r.action_type})
    unknown_verbs = [v for v in vocabulary if v not in CLOSED_VOCABULARY]
    if unknown_verbs:
        raise PolicyError(
            f"allowed_action_types names unregistered operations {unknown_verbs}"
        )

    findings = lint_rules(rules, vocabulary)
    blocking = [f for f in findings if f.severity in (Severity.ERROR, Severity.WARN)]
    if blocking:
        raise PolicyError(
            "policy is too broad to enforce safely — the scope linter blocked "
            "it:\n\n" + lint_report(findings)
        )

    budget = document.get("budget_limit")
    if budget is not None and not isinstance(budget, (int, float)):
        raise PolicyError("'budget_limit' must be a number or null")

    threshold = document.get("approval_threshold_cost", 50.0)
    if not isinstance(threshold, (int, float)):
        raise PolicyError("'approval_threshold_cost' must be a number")

    return Policy(
        agent_id=str(agent_id),
        user_id=str(user_id),
        budget_limit=float(budget) if budget is not None else None,
        approval_threshold_cost=float(threshold),
        rules=rules,
        allowed_action_types=list(vocabulary),
        source=source,
    )


def load_policy(path: str | Path) -> Policy:
    """Read and compile a policy JSON file."""
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"policy file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(f"policy file {path} is not valid JSON: {exc}") from exc
    return compile_policy(document, source=str(path))


def build_protocol(policy: Policy) -> SafetyProtocol:
    """Instantiate an enforceable protocol from a compiled policy."""
    return SafetyProtocol(
        agent_id=policy.agent_id,
        user_id=policy.user_id,
        scope_rules=policy.rules,
        budget_limit=policy.budget_limit,
        approval_threshold_cost=policy.approval_threshold_cost,
        allowed_action_types=policy.allowed_action_types,
    )


def build_from_file(path: str | Path) -> tuple[Policy, SafetyProtocol]:
    policy = load_policy(path)
    return policy, build_protocol(policy)


__all__ = [
    "Policy",
    "PolicyError",
    "build_from_file",
    "build_protocol",
    "compile_policy",
    "load_policy",
]
