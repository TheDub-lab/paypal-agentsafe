"""
Integration with PayPal's official agent toolkit.

``@paypal/agent-toolkit`` exposes PayPal's API as agent tools for OpenAI's
Agents SDK, LangChain, CrewAI, Bedrock, MCP, and Vercel's AI SDK. Handing those
tools straight to a model gives the model a spending capability with no ceiling.

This module keeps the toolkit and puts the guard in front of it. Every tool the
model can see is a wrapper whose first step is
:meth:`~paypal_guard.guard.PayPalGuard.call`. The model still chooses what to
ask for; the policy decides whether it happens. A refusal comes back to the
model as tool output, so the agent can explain itself rather than fail silently.

Usage::

    from paypal_guard import PayPalGuard, load_policy, build_protocol
    from paypal_guard.toolkit import build_toolkit, gated_tools

    policy = load_policy("policy.example.json")
    guard = PayPalGuard(build_protocol(policy))

    toolkit = build_toolkit(actions={"orders": {"create": True, "capture": True}})
    tools = gated_tools(guard, toolkit)
    # hand `tools` to your agent framework

Tool naming
-----------
The toolkit has spelled the same operation ``orders.capture``, ``orders_capture``
and ``captureOrder`` across versions. All of them resolve to the same registered
verb via :func:`paypal_guard.actions.resolve`, so an upstream rename does not
silently widen or narrow a permission. See ``tests/test_actions.py``.

Uninstalled toolkit
-------------------
Nothing here imports ``paypal_agent_toolkit`` at module scope, so this package
imports and its tests run with no PayPal dependency and no credentials. The
import happens inside :func:`build_toolkit`, and fails with an actionable
message naming the extra to install.
"""

from __future__ import annotations

import copy
import inspect
import os
from collections.abc import Callable
from typing import Any

from .actions import PayPalAction, resolve
from .guard import GuardDecision, PayPalGuard

_INSTALL_HINT = (
    "The PayPal agent toolkit is not installed. Install it with "
    "`pip install paypal-agent-toolkit` (or `pip install -r "
    "requirements-agents.txt`) to run against PayPal. The offline demo in "
    "examples/offline_demo.py needs no dependencies at all."
)


def build_toolkit(
    actions: dict | None = None,
    sandbox: bool = True,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> Any:
    """Construct ``PayPalToolkit`` from credentials in the environment.

    Args:
        actions: Which toolkit action groups to enable, e.g.
            ``{"orders": {"create": True, "capture": True, "get": True}}``.
            Keep this tight — the guard will refuse what the policy does not
            grant, but a narrower toolkit is a smaller blast radius.
        sandbox: Use the PayPal sandbox. Defaults to True; pass False only
            with real credentials you intend to spend.
        client_id: Defaults to ``PAYPAL_CLIENT_ID``.
        client_secret: Defaults to ``PAYPAL_CLIENT_SECRET``.

    Raises:
        RuntimeError: if the toolkit is not installed or credentials are absent.
    """
    client_id = client_id or os.environ.get("PAYPAL_CLIENT_ID")
    client_secret = client_secret or os.environ.get("PAYPAL_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "PayPal credentials not found. Set PAYPAL_CLIENT_ID and "
            "PAYPAL_CLIENT_SECRET, or pass them explicitly. Sandbox "
            "credentials come from the PayPal Developer Dashboard."
        )

    try:
        from paypal_agent_toolkit.openai.toolkit import PayPalToolkit
        from paypal_agent_toolkit.shared.configuration import (
            Configuration,
            Context,
        )
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(_INSTALL_HINT) from exc

    configuration = Configuration(
        actions=actions or {"orders": {"create": True, "get": True}},
        context=Context(sandbox=sandbox),
    )

    return PayPalToolkit(
        client_id=client_id,
        secret=client_secret,
        configuration=configuration,
    )


# ---------------------------------------------------------------------------
# Wrapping
# ---------------------------------------------------------------------------

#: registered verb -> (resource collection, payload keys that may carry its id)
_RESOURCE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "paypal_order_create": ("order", ()),
    "paypal_order_list": ("order", ()),
    "paypal_order_get": ("order", ("id", "order_id")),
    "paypal_order_patch": ("order", ("id", "order_id")),
    "paypal_order_capture": ("order", ("id", "order_id")),
    "paypal_order_authorize": ("order", ("id", "order_id")),
    "paypal_invoice_create": ("invoice", ()),
    "paypal_invoice_list": ("invoice", ()),
    "paypal_invoice_get": ("invoice", ("id", "invoice_id")),
    "paypal_invoice_patch": ("invoice", ("id", "invoice_id")),
    "paypal_invoice_send": ("invoice", ("id", "invoice_id")),
    "paypal_invoice_send_reminder": ("invoice", ("id", "invoice_id")),
    "paypal_invoice_cancel": ("invoice", ("id", "invoice_id")),
    "paypal_invoice_generate_qrc": ("invoice", ("id", "invoice_id")),
    "paypal_dispute_list": ("dispute", ()),
    "paypal_dispute_get": ("dispute", ("id", "dispute_id")),
    "paypal_shipment_create": ("shipment", ("id", "order_id")),
    "paypal_shipment_show": ("shipment", ("id", "order_id")),
    "paypal_shipment_cancel": ("shipment", ("id", "order_id")),
    "paypal_product_create": ("product", ()),
    "paypal_product_list": ("product", ()),
    "paypal_product_update": ("product", ("id", "product_id")),
    "paypal_subscription_plan_create": ("subscription_plan", ()),
    "paypal_subscription_plan_show": ("subscription_plan", ("id", "plan_id")),
}


def _target_for(action: PayPalAction, merchant_id: str, params: dict) -> str:
    """Build the scope target a tool call should be judged against.

    The policy is written against resources (``paypal/merchant/m1/order/*``),
    not against operations, so a rule cannot accidentally grant every order when
    it meant to grant one. Collection-wide operations resolve to a collection
    glob; operations naming a resource resolve to that resource.
    """
    collection, id_keys = _RESOURCE_MAP.get(action.name, ("unknown", ()))
    resource_id = next(
        (params[key] for key in id_keys if params.get(key)), None
    )
    suffix = str(resource_id) if resource_id else "*"
    return f"paypal/merchant/{merchant_id}/{collection}/{suffix}"


def _render(decision: GuardDecision) -> str:
    """Serialise a decision as tool output the model can read and react to."""
    lines = [f"PAYMENT GATE: {decision.outcome.upper()}"]
    lines.append(f"operation: {decision.action}")
    lines.append(f"target: {decision.target}")
    if decision.amount is not None:
        lines.append(f"amount: {decision.currency} {decision.amount:.2f}")
    if decision.reason:
        lines.append(f"reason: {decision.reason}")
    if decision.approval_token:
        lines.append(
            f"action: this payment is held for human approval (token "
            f"{decision.approval_token}). Do not retry; tell the user it awaits "
            f"sign-off."
        )
    if decision.allowed:
        lines.append("result: executed")
        lines.append(f"payload: {decision.payload}")
    else:
        lines.append(
            "result: not executed. Adjust the request to fit policy, or ask "
            "the user what they want to authorise."
        )
    return "\n".join(lines)


def gate_callable(
    guard: PayPalGuard,
    tool_name: str,
    merchant_id: str,
    inner: Callable,
    is_async: bool = False,
) -> Callable:
    """Wrap a raw callable so the gate runs before it does.

    ``inner`` receives the same arguments the framework passed. It is reached
    only when the guard returns ``allowed``.
    """
    action = resolve(tool_name)

    def _before(args: tuple, kwargs: dict) -> GuardDecision:
        if action is None:
            return guard.call(tool_name, f"paypal/merchant/{merchant_id}/unknown", dict(kwargs))
        params = dict(kwargs) if kwargs else {}
        return guard.call(action.name, _target_for(action, merchant_id, params), params)

    if is_async:
        async def async_wrapper(*args, **kwargs):
            decision = _before(args, kwargs)
            if not decision.allowed:
                return _render(decision)
            result = inner(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            decision.payload = result
            return _render(decision)
        return async_wrapper

    def wrapper(*args, **kwargs):
        decision = _before(args, kwargs)
        if not decision.allowed:
            return _render(decision)
        decision.payload = inner(*args, **kwargs)
        return _render(decision)

    return wrapper


def wrap_tool(guard: PayPalGuard, tool: Any, merchant_id: str) -> Any:
    """Wrap one framework tool object, preserving its schema.

    Handles the shapes the supported frameworks use, so the model still sees a
    valid tool definition — only the execution path changed.

    - OpenAI Agents SDK ``FunctionTool``: swaps ``on_invoke_tool``.
    - LangChain ``StructuredTool``: swaps ``func`` / ``coroutine``.
    - Vercel AI SDK: a plain dict with an ``execute`` key.
    - Anything else: an object exposing a callable attribute.
    """
    # Vercel AI SDK / dict-shaped tool spec
    if isinstance(tool, dict) and callable(tool.get("execute")):
        name = tool.get("name", "")
        wrapped = dict(tool)
        inner = tool["execute"]
        wrapped["execute"] = gate_callable(
            guard,
            name,
            merchant_id,
            inner,
            is_async=inspect.iscoroutinefunction(inner),
        )
        return wrapped

    name = getattr(tool, "name", None)

    # OpenAI Agents SDK FunctionTool
    if hasattr(tool, "on_invoke_tool") and name:
        wrapped = copy.copy(tool)
        inner = tool.on_invoke_tool

        async def on_invoke(tool_context, tool_input, _inner=inner, _name=name):
            params = dict(tool_input) if isinstance(tool_input, dict) else {}
            action = resolve(_name)
            if action is None:
                decision = guard.call(_name, "paypal/unknown", params)
            else:
                decision = guard.call(
                    action.name, _target_for(action, merchant_id, params), params
                )
            if not decision.allowed:
                return _render(decision)
            result = _inner(tool_context, tool_input)
            if inspect.isawaitable(result):
                result = await result
            return result

        wrapped.on_invoke_tool = on_invoke
        return wrapped

    # LangChain StructuredTool
    if name and (hasattr(tool, "func") or hasattr(tool, "coroutine")):
        wrapped = copy.copy(tool)
        if getattr(tool, "func", None) is not None:
            wrapped.func = gate_callable(guard, name, merchant_id, tool.func)
        if getattr(tool, "coroutine", None) is not None:
            wrapped.coroutine = gate_callable(
                guard, name, merchant_id, tool.coroutine, is_async=True
            )
        return wrapped

    # Plain callable
    if callable(tool) and name is None:
        return gate_callable(guard, getattr(tool, "__name__", ""), merchant_id, tool)

    raise TypeError(
        f"do not know how to gate tool of type {type(tool).__name__!r}. Pass an "
        f"adapter to gated_tools(), or extend wrap_tool()."
    )


def gated_tools(
    guard: PayPalGuard,
    toolkit: Any,
    merchant_id: str,
    skip: set[str] | None = None,
) -> list[Any]:
    """Wrap every tool the toolkit exposes.

    Args:
        guard: The gate. Tools call it before touching PayPal.
        toolkit: A ``PayPalToolkit`` instance.
        merchant_id: The merchant whose resources the policy is written
            against. Actions targeting anything else are denied by the scope
            allowlist.
        skip: Tool names to leave ungated. There is no good reason to pass
            this; it exists so the gap is explicit in a diff rather than
            implied.

    Returns:
        Wrapped tools, same shapes as the toolkit returned.
    """
    skip = skip or set()
    tools = toolkit.get_tools()
    gated = []
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        if name in skip:
            gated.append(tool)
            continue
        gated.append(wrap_tool(guard, tool, merchant_id))
    return gated


__all__ = [
    "build_toolkit",
    "gate_callable",
    "gated_tools",
    "wrap_tool",
]
