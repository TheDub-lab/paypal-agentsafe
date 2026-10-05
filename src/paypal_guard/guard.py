"""
The gate: a PayPal client that cannot act beyond its policy.

This is the enforcement point. An agent that wants to move money through PayPal
does not hold a PayPal client. It holds a :class:`PayPalGuard`, and the only way
to reach the network is :meth:`PayPalGuard.call`, which asks the protocol first.

    decision = guard.call("paypal_order_capture", target, {"id": "8XU8..."})

    decision.outcome   -> "pending_approval"
    decision.reason    -> "Action cost $240.00 requires human approval (threshold $50.00)"
    transport was called -> False

The invariant this class exists to hold: **the transport is reached only on an
ALLOWED outcome.** Blocked and pending actions never get as far as the HTTP
call. Not "we log and skip" — the code path that calls the transport is
downstream of the single branch that handles ``ActionOutcome.ALLOWED``.

Three ways an action can be refused, in the order they are checked:

1. **Unregistered verb.** The agent asked for something that is not one of
   PayPal's operations. Refused before any rule is consulted.
2. **Undeterminable cost.** The operation moves money but its amount could not
   be read from the payload and no resolver supplied it. We cannot cap what we
   cannot price, so we refuse.
3. **Protocol denial.** Binding, scope allowlist, per-action cap, session
   budget, or approval threshold.

Amount resolvers
----------------
``orders.capture`` carries only an order id. To price it, register a resolver
that fetched the order and returns its total:

    guard.set_amount_resolver(lambda action, params: lookup_total(params["id"]))

If the resolver cannot price the action either, the call is refused. That is
deliberate: an unpriced capture is an unbounded capture.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .actions import PayPalAction, resolve
from .amounts import AmountResult, extract_amount
from .governance import ActionOutcome, ActionRequest, ActionResult, SafetyProtocol

#: A transport is any callable that performs the real PayPal API call.
Transport = Callable[[PayPalAction, str, dict], Any]

#: An amount resolver returns the USD total for an action, or None if it
#: cannot determine one.
AmountResolver = Callable[[PayPalAction, dict], "AmountResult | None"]

_SECRET_HINTS = ("secret", "token", "password", "authorization", "credential")


def _strip_secrets(params: dict) -> dict:
    """Drop credential-shaped keys before a payload reaches the audit trail.

    A well-formed PayPal order or invoice payload carries no credentials, so
    this is a no-op in normal operation. It exists so that a sloppy agent
    cannot write a secret into a tamper-evident log by including one in a
    request body.
    """
    clean = {}
    for key, value in (params or {}).items():
        if any(hint in str(key).lower() for hint in _SECRET_HINTS):
            clean[key] = "[redacted]"
        elif isinstance(value, dict):
            clean[key] = _strip_secrets(value)
        else:
            clean[key] = value
    return clean


@dataclass
class GuardDecision:
    """What the gate decided, and why."""

    request_id: str
    action: str
    target: str
    outcome: str
    amount: float | None = None
    currency: str | None = None
    reason: str | None = None
    approval_token: str | None = None
    amount_source: str | None = None
    payload: Any = None

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"

    @property
    def blocked(self) -> bool:
        return self.outcome.startswith("blocked")

    @property
    def needs_approval(self) -> bool:
        return self.outcome == "pending_approval"

    def __str__(self) -> str:
        money = (
            f"{self.currency} {self.amount:.2f}"
            if self.amount is not None
            else "amount unknown"
        )
        line = f"[{self.outcome.upper()}] {self.action} -> {self.target} ({money})"
        if self.reason:
            line += f"\n    {self.reason}"
        if self.approval_token:
            line += f"\n    approval token: {self.approval_token}"
        return line


class PayPalGuard:
    """A policy-enforcing wrapper around a PayPal transport."""

    def __init__(
        self,
        protocol: SafetyProtocol,
        transport: Transport | None = None,
        amount_resolver: AmountResolver | None = None,
    ):
        self.protocol = protocol
        self.transport = transport
        self.amount_resolver = amount_resolver
        self.refusals: list[GuardDecision] = []

    # -- configuration ---------------------------------------------------

    def set_transport(self, transport: Transport) -> PayPalGuard:
        self.transport = transport
        return self

    def set_amount_resolver(self, resolver: AmountResolver) -> PayPalGuard:
        self.amount_resolver = resolver
        return self

    # -- emergency controls ----------------------------------------------

    def kill(self, reason: str) -> None:
        """Freeze every subsequent action, immediately and globally."""
        self.protocol.engage_killswitch(reason)

    def unfreeze(self) -> None:
        self.protocol.disengage_killswitch()

    def revoke(self, reason: str) -> None:
        """Permanently strip the agent's authority. Unfreezing will not undo it."""
        self.protocol.revoke_binding(reason)

    # -- approvals --------------------------------------------------------

    def approve(self, token: str, approver: str, reason: str | None = None) -> bool:
        """A human signs off on a pending action."""
        return self.protocol.decide_approval(token, True, approver, reason)

    def deny(self, token: str, approver: str, reason: str | None = None) -> bool:
        """A human rejects a pending action."""
        return self.protocol.decide_approval(token, False, approver, reason)

    def pending(self) -> list[dict]:
        return self.protocol.get_pending_approvals()

    # -- the gate ---------------------------------------------------------

    def _refuse(
        self,
        action: str,
        target: str,
        outcome: str,
        reason: str,
        amount: AmountResult | None = None,
        request: ActionRequest | None = None,
        monitor_outcome: str = "blocked",
    ) -> GuardDecision:
        """Record a refusal that ``SafetyProtocol.execute`` did not produce.

        Three of the gate's refusals happen before there is anything to execute:
        an unregistered verb, an action outside the scope allowlist, and a money
        movement whose amount cannot be determined. None of them can be expressed
        as the outcome of a rule evaluation — there is no rule for an unknown
        verb, and no honest cost to compare a cap against.

        They are still recorded in the same audit trail, with the same hash chain
        and the same monitor counters, because an attempt that was refused is
        exactly the evidence an operator needs. They are counted as blocked
        actions so that a policy under attack is visible in the same counter as
        an ordinary scope denial.
        """
        result = ActionResult(
            request.request_id if request else "-",
            ActionOutcome.BLOCKED_SCOPE,
            block_reason=reason,
        )
        decision = GuardDecision(
            request_id=result.request_id,
            action=action,
            target=target,
            outcome=outcome,
            amount=amount.amount if amount else None,
            currency=amount.currency if amount else None,
            amount_source=amount.source if amount else None,
            reason=reason,
        )
        self.protocol.audit.append("action_refused", self.protocol.agent_id, {
            "outcome": outcome,
            "action_type": action,
            "target": target,
            "amount": decision.amount,
            "currency": decision.currency,
            "reason": reason,
        })
        self.protocol.monitor.action_count += 1
        if monitor_outcome == "blocked":
            self.protocol.monitor.blocked_count += 1
        self.refusals.append(decision)
        return decision

    def call(
        self,
        action: str,
        target: str,
        params: dict | None = None,
        urgency: str = "normal",
    ) -> GuardDecision:
        """Ask the protocol for permission, then perform the action if allowed.

        Args:
            action: A registered PayPal operation, e.g. ``paypal_order_capture``.
            target: The PayPal resource being acted on, e.g.
                ``paypal/merchant/<id>/order/<order_id>``.
            params: The request payload.
            urgency: ``"critical"`` forces the approval gate even when the
                amount is small. Use sparingly — it is a request for a human,
                not a way to skip one.

        Returns:
            A :class:`GuardDecision`. ``payload`` is populated only when the
            action was allowed and a transport was configured.
        """
        params = params or {}
        clean_params = _strip_secrets(params)

        # 1. Closed vocabulary. An unregistered verb has no rule to satisfy,
        #    and scope is an allowlist — so the answer can only be no.
        registered = resolve(action)
        if registered is None:
            return self._refuse(
                action,
                target,
                "blocked_unknown_action",
                f"'{action}' is not a registered PayPal operation. The agent's "
                f"action vocabulary is closed; unregistered verbs are refused "
                f"regardless of scope.",
            )

        # 2. Is this action even in scope? Settled before we spend anything on
        #    its behalf — a priced lookup is a real API call, and firing one for
        #    an order belonging to a merchant the agent was never granted is its
        #    own small authorisation failure. Scope needs no amount, so it can
        #    be answered first, and the refusal reason stays truthful.
        amount = extract_amount(registered, clean_params)
        preflight = ActionRequest(
            action_type=registered.name,
            target=target,
            params=clean_params,
            method=registered.method,
            estimated_cost=0.0,
        )

        # A frozen or revoked protocol short-circuits straight to ``execute``,
        # which blocks it. Nothing else runs: no scope evaluation, and above all
        # no amount resolver, because a freeze is supposed to mean the agent
        # stops touching PayPal rather than keeps issuing lookups against a
        # fraud alert. ``execute`` records the refusal and returns before budget
        # or approval are touched, so there is nothing to unwind.
        suspended = self.protocol.frozen or self.protocol.revoked

        if not suspended:
            scope_violation = self.protocol.check_scope_only(preflight)
            if scope_violation:
                return self._refuse(
                    action,
                    target,
                    "blocked_scope",
                    scope_violation,
                    amount=amount,
                    request=preflight,
                )

            # 3. Price the action. Fail closed if we cannot. The resolver may be
            #    a live orders.get, so it runs only once scope has cleared.
            if registered.money_moving and not amount.known and self.amount_resolver:
                resolved = self.amount_resolver(registered, clean_params)
                if resolved is not None and resolved.known:
                    amount = resolved

            if registered.money_moving and not amount.known:
                return self._refuse(
                    action,
                    target,
                    "blocked_unknown_amount",
                    f"cannot determine the cost of '{action}' and therefore "
                    f"cannot authorise it: {amount.error}. Register an amount "
                    f"resolver to supply the figure from PayPal, or send a "
                    f"payload whose amount is explicit.",
                    amount=amount,
                )

        request = ActionRequest(
            action_type=registered.name,
            target=target,
            params=clean_params,
            method=registered.method,
            estimated_cost=amount.amount or 0.0,
            urgency=urgency,
        )

        # 3. The protocol decides.
        result = self.protocol.execute(request)

        decision = GuardDecision(
            request_id=result.request_id,
            action=registered.name,
            target=target,
            outcome=result.outcome.value,
            amount=amount.amount,
            currency=amount.currency,
            amount_source=amount.source,
            reason=result.block_reason,
            approval_token=result.requires_approval_for,
        )

        if result.outcome is not ActionOutcome.ALLOWED:
            return decision

        # 4. Allowed. This is the only path to the transport.
        if self.transport is not None:
            decision.payload = self.transport(registered, target, clean_params)
        return decision

    # -- introspection ----------------------------------------------------

    def permitted_operations(self) -> list[str]:
        """The verbs this agent may attempt, whether or not a rule grants them."""
        return list(self.protocol.allowed_action_types)

    def status(self) -> dict:
        return {
            "binding": self.protocol.binding,
            "monitor": self.protocol.monitor.get_status(),
            "pending_approvals": self.protocol.get_pending_approvals(),
            "audit_entries": len(self.protocol.audit.query()),
            "audit_broken_links": self.protocol.audit.verify_integrity(),
            "pre_protocol_refusals": len(self.refusals),
        }

    def audit_report(self) -> str:
        """Human-readable, reconstructable history of everything attempted."""
        return self.protocol.audit.reconstruct_sequence(self.protocol.agent_id)


# ---------------------------------------------------------------------------
# Target builders — keep policy files readable instead of full of string concat
# ---------------------------------------------------------------------------

def merchant_root(merchant_id: str) -> str:
    return f"paypal/merchant/{merchant_id}"


def order_target(merchant_id: str, order_id: str) -> str:
    return f"paypal/merchant/{merchant_id}/order/{order_id}"


def invoice_target(merchant_id: str, invoice_id: str) -> str:
    return f"paypal/merchant/{merchant_id}/invoice/{invoice_id}"


def dispute_target(merchant_id: str, dispute_id: str) -> str:
    return f"paypal/merchant/{merchant_id}/dispute/{dispute_id}"


def order_collection(merchant_id: str) -> str:
    return f"paypal/merchant/{merchant_id}/order/*"


def invoice_collection(merchant_id: str) -> str:
    return f"paypal/merchant/{merchant_id}/invoice/*"


__all__ = [
    "AmountResolver",
    "GuardDecision",
    "PayPalGuard",
    "Transport",
    "dispute_target",
    "invoice_collection",
    "invoice_target",
    "merchant_root",
    "order_collection",
    "order_target",
]
