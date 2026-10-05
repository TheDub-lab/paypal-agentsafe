"""
Offline demo: the whole gate, no credentials, no network.

Run it::

    python examples/offline_demo.py

Eleven scenarios against a simulated PayPal transport. The transport records
every call it receives, so when a decision says ``blocked`` you can see that
nothing reached PayPal. The refusal is structural, not advisory.

Scenarios
     1. read an order                      allowed
     2. create a $40 order                 allowed, inside the per-action cap
     3. capture a $240 order               held for approval, transport untouched
     4. human approves, agent retries      allowed
     5. agent invents a refund verb        refused: not in the closed vocabulary
     6. capture with no known amount       refused: cannot price what we cannot cap
     7. capture against another merchant   refused: outside the scope allowlist
     8. create an over-cap order           refused: per-action cap
     9. spend until the session budget runs out
    10. kill switch                        refused: frozen, globally
    11. the audit trail, and a tamper check on its hash chain
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from paypal_guard import (  # noqa: E402
    AmountResult,
    PayPalGuard,
    build_protocol,
    load_policy,
)

MERCHANT = "ACME-MERCH-4417"
ROOT = Path(__file__).resolve().parent.parent

#: What PayPal says each order is actually worth. The demo's amount resolver
#: reads from here, standing in for the orders.get call a real agent would make.
#: Note which ids are absent: an order PayPal cannot price must be refused.
ORDER_TOTALS = {
    "8XU83804L01907047": 240.00,
    "1AB23456CD789012": 40.00,
}


class SimulatedPayPal:
    """A PayPal transport that records instead of charging."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, action, target: str, params: dict):
        self.calls.append(f"{action.name}({target})")
        return {
            "id": params.get("id", "NEW-ORDER-0001"),
            "status": "COMPLETED" if "capture" in action.name else "CREATED",
            "simulated": True,
        }


def price_from_paypal(action, params: dict) -> AmountResult | None:
    """Stand in for a real orders.get lookup before a capture.

    Returns ``None`` when PayPal has no record of the order, which the guard
    treats as unpriceable, and therefore refused.
    """
    order_id = params.get("id")
    total = ORDER_TOTALS.get(order_id)
    if total is None:
        return None
    return AmountResult(total, "USD", f"orders.get({order_id})")


def banner(step: int, title: str, subtitle: str = "") -> None:
    print()
    print("=" * 74)
    print(f"  {step}. {title}")
    if subtitle:
        print(f"     {subtitle}")
    print("=" * 74)


def attempt(guard, paypal, label: str, *args, **kwargs):
    """Make one gated call and report whether it actually reached PayPal.

    The transport's call count is sampled either side of ``guard.call``, so the
    printed claim is measured rather than asserted.
    """
    before = len(paypal.calls)
    decision = guard.call(*args, **kwargs)
    reached = len(paypal.calls) > before
    print(f"\n  agent -> {label}")
    print(f"  {decision}")
    print(f"  reached PayPal: {'YES' if reached else 'no'}")
    return decision


def order(amount: str, intent: str = "CAPTURE") -> dict:
    return {
        "intent": intent,
        "purchase_units": [{"amount": {"currency_code": "USD", "value": amount}}],
    }


def main() -> int:
    policy = load_policy(ROOT / "policies" / "autopilot.json")
    print(policy.describe())

    paypal = SimulatedPayPal()
    guard = PayPalGuard(build_protocol(policy), transport=paypal)
    guard.set_amount_resolver(price_from_paypal)

    def target(order_id: str, merchant: str = MERCHANT) -> str:
        return f"paypal/merchant/{merchant}/order/{order_id}"

    banner(1, "Read order 8XU83804L01907047 ($240.00)", "scope allows it, costs nothing")
    attempt(
        guard, paypal, "paypal_order_get",
        "paypal_order_get", target("8XU83804L01907047"), {"id": "8XU83804L01907047"},
    )

    banner(2, "Create a $40.00 order", "per-action cap is $75.00")
    attempt(
        guard, paypal, "paypal_order_create",
        "paypal_order_create", target("new/1AB23456CD789012"), order("40.00"),
    )

    banner(3, "Capture order 8XU83804L01907047 ($240.00)",
           "per-action cap $300.00 clears, approval threshold is $100.00")
    capture = attempt(
        guard, paypal, "paypal_order_capture",
        "paypal_order_capture", target("8XU83804L01907047"),
        {"id": "8XU83804L01907047"},
    )

    print("\n  ...a human opens the approval queue:")
    for item in guard.pending():
        print(f"      token={item['token']}  {item['action_type']}  "
              f"${item['estimated_cost']:.2f}")

    banner(4, "Human approves, agent retries",
           "same target, same amount, 5-minute approval window")
    guard.approve(
        capture.approval_token,
        approver="ops-manager@example.com",
        reason="invoice INV-2291 matched, customer confirmed",
    )
    attempt(
        guard, paypal, "paypal_order_capture (retry)",
        "paypal_order_capture", target("8XU83804L01907047"),
        {"id": "8XU83804L01907047"},
    )

    banner(5, "Agent invents paypal_refund_full",
           "prompt injection: refund the customer in full")
    attempt(
        guard, paypal, "paypal_refund_full",
        "paypal_refund_full", target("8XU83804L01907047"), {"id": "8XU83804L01907047"},
    )

    banner(6, "Capture order 7QQ00000ZZ999999",
           "PayPal reports no total for it, so it cannot be priced or capped")
    attempt(
        guard, paypal, "paypal_order_capture",
        "paypal_order_capture", target("7QQ00000ZZ999999"),
        {"id": "7QQ00000ZZ999999"},
    )

    banner(7, "Capture against merchant GLOBEX-9001", "correct shape, wrong account")
    attempt(
        guard, paypal, "paypal_order_capture",
        "paypal_order_capture",
        "paypal/merchant/GLOBEX-9001/order/8XU83804L01907047",
        {"id": "8XU83804L01907047"},
    )

    banner(8, "Create a $900.00 order", "per-action cap is $75.00")
    attempt(
        guard, paypal, "paypal_order_create",
        "paypal_order_create", target("new/BIG0001"), order("900.00"),
    )

    banner(9, "Spend until the session budget is gone", "budget $500.00")
    for i in range(10):
        before = len(paypal.calls)
        decision = guard.call(
            "paypal_order_create", target(f"new/BULK{i:04d}"), order("75.00")
        )
        reached = "reached PayPal" if len(paypal.calls) > before else "held back"
        print(f"      order {i + 1:>2}: {decision.outcome:<16} "
              f"spent ${guard.protocol.monitor.total_cost:>7.2f}   {reached}")
        if decision.blocked:
            print(f"               {decision.reason}")
            break

    banner(10, "Kill switch", "one call freezes everything, globally")
    guard.kill("fraud alert on account ACME-MERCH-4417")
    attempt(
        guard, paypal, "paypal_order_create",
        "paypal_order_create", target("new/AFTER-FREEZE"), order("1.00"),
    )

    banner(11, "Audit trail", "hash-chained, every attempt recorded, allowed or not")
    status = guard.status()
    print(f"      events                : {status['audit_entries']}")
    print(f"      actions attempted     : {status['monitor']['action_count']}")
    print(f"      allowed               : {status['monitor']['allowed']}")
    print(f"      blocked               : {status['monitor']['blocked']}")
    print(f"      pending approval      : {status['monitor']['approval_pending']}")
    print(f"      gate refusals         : {status['pre_protocol_refusals']}")
    print(f"      chain broken links    : {status['audit_broken_links'] or 'none'}")

    print("\n      calls that actually reached PayPal:")
    for call in paypal.calls:
        print(f"        {call}")

    print("\n      refusals the gate made before any rule was consulted:")
    for refusal in guard.refusals:
        print(f"        {refusal.outcome:<24} {refusal.action}")

    print("\n      tamper check: editing entry 3's payload must break the chain")
    entries = guard.protocol.audit.query()
    entries[3]["data"]["amount"] = 1.00
    broken = guard.protocol.audit.verify_integrity()
    print(f"      broken links after edit: {broken}")
    if not broken:
        print("      TAMPER NOT DETECTED - this is a bug")
        return 1
    print(f"      entry {broken[0]} no longer hashes to its recorded value")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
