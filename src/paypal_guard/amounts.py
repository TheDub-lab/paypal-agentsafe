"""
Fail-closed extraction of USD amounts from PayPal payloads.

Every budget cap, per-action cap, and approval threshold in this project is
expressed in dollars. That only works if we know what the agent is asking to
spend — and we must not take the agent's word for it.

So amounts are read from the request payload using the documented shape of each
PayPal operation, and when the shape is not recognised the extraction fails
loudly instead of defaulting to zero.

Why fail closed
---------------
Defaulting an unknown amount to ``0.00`` is the single most dangerous line of
code in a payment guard. A model that cannot state the amount, or that hides it
in a field this module does not recognise, would sail past every cap and every
approval threshold. That is the whole attack this project defends against.

The correct behaviour is to refuse: an operation whose cost cannot be
determined is not one we can authorise.

The hard case
-------------
``orders.capture`` takes only an order id — the amount is not in the request at
all. The honest answer is not to guess. :func:`extract_amount` reports the
amount as unknown, and the guard is expected to supply a resolver that looked
the amount up first (see :meth:`paypal_guard.guard.PayPalGuard.set_amount_resolver`).
Capture without a known amount is blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .actions import PayPalAction


class AmountError(ValueError):
    """Raised when a payload's currency or value cannot be parsed."""


@dataclass(frozen=True)
class AmountResult:
    """The outcome of an amount extraction.

    ``amount`` is ``None`` when we could not determine it. Callers must treat
    that as a denial for money-moving operations, never as ``0.00``.
    """

    amount: float | None
    currency: str | None
    source: str
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.amount is not None

    def __str__(self) -> str:
        if self.known:
            return f"{self.currency} {self.amount:.2f} (from {self.source})"
        return f"unknown (looked in {self.source}): {self.error}"


def _money_node(node: Any, source: str) -> tuple[float, str]:
    """Parse a PayPal money object: ``{"currency_code": "USD", "value": "1.00"}``."""
    if not isinstance(node, dict):
        raise AmountError(f"{source} is {type(node).__name__}, not a money object")

    raw_value = node.get("value")
    currency = node.get("currency_code") or node.get("currency")
    if raw_value is None:
        raise AmountError(f"{source} has no 'value' field")
    if currency is None:
        raise AmountError(f"{source} has no 'currency_code'")

    try:
        amount = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise AmountError(f"{source} value {raw_value!r} is not a number") from exc

    if amount < 0:
        raise AmountError(f"{source} value {raw_value!r} is negative")

    return float(amount), str(currency).upper()


def _order_total(params: dict, source: str) -> tuple[float, str]:
    """Total an Orders v2 ``purchase_units`` array.

    Refuses to sum across currencies. Two units in different currencies is not
    an amount we can compare against a USD budget, and guessing which one the
    budget meant is exactly the kind of ambiguity a guard must not resolve on
    the model's behalf.
    """
    units = params.get("purchase_units")
    if not isinstance(units, list) or not units:
        raise AmountError(f"{source}: 'purchase_units' is missing or empty")

    total = 0.0
    currency: str | None = None
    for i, unit in enumerate(units):
        unit_amount, unit_currency = _money_node(
            (unit or {}).get("amount"), f"{source}.purchase_units[{i}].amount"
        )
        if currency is None:
            currency = unit_currency
        elif unit_currency != currency:
            raise AmountError(
                f"{source}: mixed currencies ({currency} and {unit_currency}) "
                f"cannot be summed against a single-currency budget"
            )
        total += unit_amount

    return total, str(currency)


def extract_amount(action: PayPalAction, params: dict) -> AmountResult:
    """Determine what ``action`` costs, or report that we cannot.

    Args:
        action: The registered PayPal operation being attempted.
        params: The request payload the agent produced.

    Returns:
        An :class:`AmountResult`. For read operations this is always ``0.00``.
        For money-moving operations it is the amount when the documented
        payload shape is present, and ``amount=None`` otherwise.
    """
    params = params or {}

    if not action.money_moving:
        return AmountResult(0.0, None, "read-only operation", None)

    name = action.name

    try:
        if name in ("paypal_order_create", "paypal_order_patch"):
            amount, currency = _order_total(params, name)
            return AmountResult(amount, currency, f"{name}.purchase_units")

        if name in (
            "paypal_invoice_create",
            "paypal_invoice_patch",
            "paypal_invoice_send",
            "paypal_invoice_send_reminder",
            "paypal_invoice_generate_qrc",
        ):
            amount, currency = _money_node(params.get("amount"), f"{name}.amount")
            return AmountResult(amount, currency, f"{name}.amount")

    except AmountError as exc:
        return AmountResult(None, None, name, str(exc))

    return AmountResult(
        None,
        None,
        name,
        "amount is not present in this operation's request payload; it must be "
        "resolved from PayPal before the action can be authorised",
    )


__all__ = ["AmountError", "AmountResult", "extract_amount"]
