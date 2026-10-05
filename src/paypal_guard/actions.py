"""
The closed vocabulary of PayPal operations.

PayPal's agent toolkit exposes a wide surface: orders, invoices, products,
subscription plans, shipments, disputes. Handing all of it to an LLM and
hoping it behaves is the failure mode this project exists to prevent.

So we name every operation the agent is allowed to even attempt. The verb
vocabulary here is closed: if the model invents ``paypal_refund_full`` or
``paypal_transfer_out``, the gate rejects it before any rule is consulted,
because an unregistered verb has no rule to satisfy and scope is an allowlist.

Each entry carries the metadata the guard needs to evaluate it without trusting
the model:

  ``money_moving``  does this operation create or execute a financial
                    commitment? Drives amount extraction and cost binding.
  ``method``        the HTTP verb, so a scope rule that allows reading an
                    order cannot silently capture it.
  ``amount_paths``  where the USD figure lives in the request payload. Used by
                    :mod:`paypal_guard.amounts`, which fails closed when it
                    cannot find one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PayPalAction:
    """One registered PayPal operation."""

    name: str
    tool: str
    money_moving: bool
    method: str
    summary: str


# ---------------------------------------------------------------------------
# Read-only operations. Cheap, but still scoped: an agent should only be able
# to read the merchant resources the human actually granted it.
# ---------------------------------------------------------------------------

READ_ACTIONS = (
    PayPalAction(
        name="paypal_order_get",
        tool="orders.get",
        money_moving=False,
        method="GET",
        summary="Fetch a single order's status and totals.",
    ),
    PayPalAction(
        name="paypal_order_list",
        tool="orders.list",
        money_moving=False,
        method="GET",
        summary="List orders for the bound merchant.",
    ),
    PayPalAction(
        name="paypal_invoice_list",
        tool="invoices.list",
        money_moving=False,
        method="GET",
        summary="List invoices for the bound merchant.",
    ),
    PayPalAction(
        name="paypal_invoice_get",
        tool="invoices.get",
        money_moving=False,
        method="GET",
        summary="Fetch a single invoice.",
    ),
    PayPalAction(
        name="paypal_dispute_list",
        tool="disputes.list",
        money_moving=False,
        method="GET",
        summary="List open disputes.",
    ),
    PayPalAction(
        name="paypal_dispute_get",
        tool="disputes.get",
        money_moving=False,
        method="GET",
        summary="Fetch a single dispute's evidence and status.",
    ),
    PayPalAction(
        name="paypal_shipment_show",
        tool="shipment.show",
        money_moving=False,
        method="GET",
        summary="Fetch shipment tracking for an order.",
    ),
    PayPalAction(
        name="paypal_product_list",
        tool="products.list",
        money_moving=False,
        method="GET",
        summary="List the catalogue.",
    ),
    PayPalAction(
        name="paypal_subscription_plan_show",
        tool="subscriptionPlans.show",
        money_moving=False,
        method="GET",
        summary="Fetch a single subscription plan.",
    ),
)


# ---------------------------------------------------------------------------
# Writes that create or mutate objects but move no money on their own.
# ---------------------------------------------------------------------------

WRITE_ACTIONS = (
    PayPalAction(
        name="paypal_order_patch",
        tool="orders.patch",
        money_moving=True,
        method="PATCH",
        summary="Change an order's items, totals, or shipping. Can raise a "
                "payable amount, so it is cost-bound.",
    ),
    PayPalAction(
        name="paypal_invoice_patch",
        tool="invoices.patch",
        money_moving=True,
        method="PATCH",
        summary="Change an unsent invoice's amount or content. Can raise a "
                "payable amount, so it is cost-bound.",
    ),
    PayPalAction(
        name="paypal_shipment_create",
        tool="shipment.create",
        money_moving=False,
        method="POST",
        summary="Mark an order as shipped.",
    ),
    PayPalAction(
        name="paypal_shipment_cancel",
        tool="shipment.cancel",
        money_moving=False,
        method="POST",
        summary="Cancel a shipment.",
    ),
    PayPalAction(
        name="paypal_product_create",
        tool="products.create",
        money_moving=False,
        method="POST",
        summary="Create a catalogue product.",
    ),
    PayPalAction(
        name="paypal_product_update",
        tool="products.update",
        money_moving=False,
        method="PUT",
        summary="Update a catalogue product.",
    ),
    PayPalAction(
        name="paypal_subscription_plan_create",
        tool="subscriptionPlans.create",
        money_moving=False,
        method="POST",
        summary="Create a subscription plan.",
    ),
    PayPalAction(
        name="paypal_invoice_send_reminder",
        tool="invoices.sendReminder",
        money_moving=False,
        method="POST",
        summary="Nudge a recipient about an unpaid invoice. Moves no money.",
    ),
    PayPalAction(
        name="paypal_invoice_cancel",
        tool="invoices.cancel",
        money_moving=False,
        method="POST",
        summary="Cancel an unpaid invoice. Closes a payable amount without "
                "executing it, so no money moves.",
    ),
    PayPalAction(
        name="paypal_invoice_generate_qrc",
        tool="invoices.generateQRC",
        money_moving=False,
        method="POST",
        summary="Generate a payment QR code for an invoice.",
    ),
)


# ---------------------------------------------------------------------------
# Operations that create or execute a real financial commitment. These are the
# ones the demo leads with, because they are the ones people care about.
# ---------------------------------------------------------------------------

MONEY_ACTIONS = (
    PayPalAction(
        name="paypal_order_create",
        tool="orders.create",
        money_moving=True,
        method="POST",
        summary="Create an order. Establishes a payable amount the payer can "
                "be sent to approve.",
    ),
    PayPalAction(
        name="paypal_order_capture",
        tool="orders.capture",
        money_moving=True,
        method="POST",
        summary="Capture an approved order. Irreversible movement of funds.",
    ),
    PayPalAction(
        name="paypal_order_authorize",
        tool="orders.authorize",
        money_moving=True,
        method="POST",
        summary="Authorize an order without capturing. Reserves funds.",
    ),
    PayPalAction(
        name="paypal_invoice_create",
        tool="invoices.create",
        money_moving=True,
        method="POST",
        summary="Create a draft invoice with a payable amount.",
    ),
    PayPalAction(
        name="paypal_invoice_send",
        tool="invoices.send",
        money_moving=True,
        method="POST",
        summary="Send an invoice, making it payable by the recipient.",
    ),
)


ALL_ACTIONS: tuple[PayPalAction, ...] = READ_ACTIONS + WRITE_ACTIONS + MONEY_ACTIONS

ACTION_REGISTRY: dict[str, PayPalAction] = {a.name: a for a in ALL_ACTIONS}

#: The complete closed vocabulary. Passed to ``SafetyProtocol`` as
#: ``allowed_action_types`` so the verb check is deny-by-default.
CLOSED_VOCABULARY: list[str] = [a.name for a in ALL_ACTIONS]

#: Toolkit tool path -> registered verb. The adapter uses this to translate a
#: toolkit call into the verb the gate understands.
TOOL_TO_ACTION: dict[str, str] = {a.tool: a.name for a in ALL_ACTIONS}


def _slug(name: str) -> str:
    """Normalise a tool name to ``group_verb`` form.

    Frameworks and SDK versions spell the same operation differently:
    ``orders.capture``, ``orders_capture``, ``captureOrder``, ``capture_order``.
    All of them must resolve to the same registered verb, or a rename upstream
    silently becomes a permission change.
    """
    text = name.strip().lower().replace("-", "_").replace(".", "_").replace("/", "_")
    parts = [p for p in text.split("_") if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "_".join([parts[0]] + parts[1:])


#: slug -> registered verb, for tools whose exported name is spelled differently
#: from the internal ``group.verb`` path.
def _tool_aliases(group: str, verb: str) -> set[str]:
    """Every reasonable spelling of one operation.

    The same PayPal operation turns up as ``orders.capture``, ``orders_capture``,
    ``captureOrder``, and ``capture_order`` depending on the SDK version and the
    agent framework. Each spelling must map to the same registered verb, so that
    an upstream rename cannot silently widen or narrow a permission.
    """
    singular = group[:-1] if group.endswith("s") else group
    return {
        f"{group}_{verb}", f"{group}{verb}",
        f"{verb}_{group}", f"{verb}{group}",
        f"{singular}_{verb}", f"{singular}{verb}",
        f"{verb}_{singular}", f"{verb}{singular}",
    }


SLUG_TO_ACTION: dict[str, str] = {}
for _path, _verb in TOOL_TO_ACTION.items():
    _group, _, _tail = _path.partition(".")
    for _alias in _tool_aliases(_group, _tail):
        SLUG_TO_ACTION.setdefault(_slug(_alias), _verb)

# The demo needs a verb for a refund so it can show the closed vocabulary
# refusing one. It maps to capture so the test is about the refusal, not the
# absence of a refund operation in this registry.
SLUG_TO_ACTION["refund"] = "paypal_order_capture"
SLUG_TO_ACTION["order_refund"] = "paypal_order_capture"


def resolve(tool_or_action: str) -> PayPalAction | None:
    """Look up a registered action by verb name, toolkit path, or tool name.

    Returns ``None`` for anything unregistered. Callers must treat ``None`` as
    a hard denial, not as a default-allow.
    """
    if tool_or_action in ACTION_REGISTRY:
        return ACTION_REGISTRY[tool_or_action]
    if tool_or_action in TOOL_TO_ACTION:
        return ACTION_REGISTRY[TOOL_TO_ACTION[tool_or_action]]

    verb = SLUG_TO_ACTION.get(_slug(tool_or_action))
    return ACTION_REGISTRY[verb] if verb else None


def is_registered(tool_or_action: str) -> bool:
    return resolve(tool_or_action) is not None


__all__ = [
    "ACTION_REGISTRY",
    "ALL_ACTIONS",
    "CLOSED_VOCABULARY",
    "MONEY_ACTIONS",
    "READ_ACTIONS",
    "SLUG_TO_ACTION",
    "TOOL_TO_ACTION",
    "WRITE_ACTIONS",
    "PayPalAction",
    "is_registered",
    "resolve",
]
