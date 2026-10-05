"""
paypal_guard — enforceable governance for AI agents that spend money via PayPal.

PayPal ships an agent toolkit that lets a model create orders, send invoices,
and capture payments. This package puts a policy gate in front of it: a closed
vocabulary of operations, deny-by-default scope over specific merchants and
resources, per-action and per-session spend caps, human approval thresholds, a
kill switch, and a hash-chained audit trail of every attempt.

    from paypal_guard import PayPalGuard, build_protocol, load_policy

    policy = load_policy("policy.example.json")
    guard = PayPalGuard(build_protocol(policy))
    decision = guard.call("paypal_order_capture", target, {"id": "8XU83804L01907047"})

    print(decision)

Three refusals, in the order they are checked: an operation outside the closed
vocabulary, a money movement whose amount cannot be determined, and anything the
policy denies. The transport is reached only on ``allowed``.

The governance core in ``paypal_guard.governance`` is vendored from
AgentCover Sovereign (MIT) so this repository stands alone. See ``NOTICE``.
"""

from .actions import (
    ALL_ACTIONS,
    CLOSED_VOCABULARY,
    PayPalAction,
    is_registered,
    resolve,
)
from .amounts import AmountError, AmountResult, extract_amount
from .governance import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
    AuditTrail,
    BoundAgent,
    Monitor,
    ProtocolState,
    SafetyProtocol,
    ScopeRule,
    Severity,
    lint_report,
    lint_rules,
)
from .guard import (
    GuardDecision,
    PayPalGuard,
    dispute_target,
    invoice_collection,
    invoice_target,
    merchant_root,
    order_collection,
    order_target,
)
from .policy import (
    Policy,
    PolicyError,
    build_from_file,
    build_protocol,
    compile_policy,
    load_policy,
)

__version__ = "0.1.0"

__all__ = [
    "ALL_ACTIONS",
    "CLOSED_VOCABULARY",
    "ActionOutcome",
    "ActionRequest",
    "ActionResult",
    "AmountError",
    "AmountResult",
    "AuditTrail",
    "BoundAgent",
    "GuardDecision",
    "Monitor",
    "PayPalAction",
    "PayPalGuard",
    "Policy",
    "PolicyError",
    "ProtocolState",
    "SafetyProtocol",
    "ScopeRule",
    "Severity",
    "__version__",
    "build_from_file",
    "build_protocol",
    "compile_policy",
    "dispute_target",
    "extract_amount",
    "invoice_collection",
    "invoice_target",
    "is_registered",
    "lint_report",
    "lint_rules",
    "load_policy",
    "merchant_root",
    "order_collection",
    "order_target",
    "resolve",
]
