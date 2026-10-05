"""
Governance core — vendored from AgentCover Sovereign.

Provenance
----------
These three modules (``core``, ``protocol``, ``scope_linter``) are copies of the
safety-protocol governance engine from the Nebius x NVIDIA Global AI Hackathon
submission, ``agentcover-sovereign`` (MIT). They are vendored here
deliberately, as a dependency-free (stdlib-only) subset, so that this PayPal
submission is self-contained: a judge can clone one repo and run it without
also cloning or trusting the other project.

Divergences from the original
-----------------------------
Four changes, all of them small and all of them deliberate. The first three were
found by running the offline demo, not by reading the code.

1. ``core.AuditTrail.verify_integrity`` checked only that each entry's
   ``prev_hash`` matched the previous entry's hash. Editing an entry's ``data``
   in place left every link and every stored hash intact, so the
   "tamper-evident" claim did not hold. It now recomputes each entry's hash from
   its contents and compares, so an edited payload is caught.

2. ``protocol.SafetyProtocol.decide_approval`` charged ``estimated_cost`` against
   the session budget on approval, and ``execute`` charged it again on the
   retry. An approved $240 capture consumed $480, so the retry was then refused
   against a budget the operator never spent. Approval no longer charges;
   execution does. The same method also recorded approvals through the monitor,
   which double-counted the intent in the running total; approvals are human
   decisions, not agent actions, so only denials are recorded there.

3. ``protocol.SafetyProtocol.check_scope_only`` is new, along with the
   ``frozen`` and ``revoked`` properties. ``execute`` interleaves scope, budget
   and approval, so a caller could not ask the scope question on its own.
   :class:`paypal_guard.guard.PayPalGuard` needs to: it must know whether an
   action is in scope *before* firing an amount resolver, because that resolver
   is a live ``orders.get`` and calling it for a merchant the agent was never
   granted is its own authorisation failure. Likewise, once the kill switch is
   engaged the guard must be able to see that without calling ``execute``, so it
   can skip the lookup entirely rather than issue PayPal reads during a fraud
   freeze. The method and the properties are pure.

4. ``ruff check --fix`` removed two unused ``typing`` imports and dropped a
   redundant ``f`` prefix from four strings. No behaviour change. Style-only
   rules are excluded for this package now, so a future lint run cannot rewrite
   vendored code and quietly widen the diff from upstream.

None of these change the framework's design or its public API. See ``NOTICE`` at
the repository root.

Original work: Michael / TheDub-lab — agentcover-sovereign, MIT License.
See the NOTICE file at the repository root for the full notice.

What this package adds
----------------------
Nothing. The enforcement semantics — binding, closed verb vocabulary, deny-by-
default scope allowlists, per-rule cost caps, global budget, approval gates,
kill switch, hash-chained audit — are all here.

The PayPal-specific work lives one level up, in ``paypal_guard``:

  - ``actions``    the closed vocabulary of PayPal operations
  - ``amounts``    fail-closed extraction of USD amounts from PayPal payloads
  - ``policy``     a PayPal policy document compiled to ``ScopeRule``s
  - ``guard``      the adapter that sits between an agent and the PayPal API

That split is the thesis of this submission: the governance engine is generic,
and generic governance is only as good as the specificity of what it governs.
PayPal's order/invoice/refund/dispute surface is the thing worth governing.
"""

from .core import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
    ApprovalRecord,
    AuditTrail,
    MatchKind,
    Monitor,
    ProtocolState,
    ScopeRule,
    normalize_target,
    target_matches,
    validate_params,
)
from .protocol import BoundAgent, SafetyProtocol
from .scope_linter import Finding, Severity, lint_report, lint_rules

__all__ = [
    "ActionOutcome",
    "ActionRequest",
    "ActionResult",
    "ApprovalRecord",
    "AuditTrail",
    "BoundAgent",
    "Finding",
    "MatchKind",
    "Monitor",
    "ProtocolState",
    "SafetyProtocol",
    "ScopeRule",
    "Severity",
    "lint_report",
    "lint_rules",
    "normalize_target",
    "target_matches",
    "validate_params",
]
