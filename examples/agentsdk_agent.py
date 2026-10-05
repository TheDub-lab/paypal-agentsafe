"""
Live example: a gated settlement agent on the PayPal sandbox.

This is the integration, not the demo. ``examples/offline_demo.py`` proves the
gate's logic with no credentials; this proves the gate holds when a real model
is choosing tools and real PayPal money is one signature away.

Setup::

    pip install -r requirements-agents.txt
    cp .env.example .env      # fill in SANDBOX credentials
    python examples/agentsdk_agent.py --ask "capture order 8XU83804L01907047"

What this shows that the offline demo cannot
-------------------------------------------
The agent is a language model choosing which tool to call. The gate is not a
prompt, and the model is not asked to behave. When the model reaches for a tool
it was never granted, or for a capture above the approval threshold, the call is
refused in infrastructure and the refusal is handed back to the model as tool
output, so the agent explains the refusal instead of failing opaquely.

The amount resolver is a real ``orders.get`` against the PayPal sandbox. This is
the part that makes a capture safe to cap: the figure the gate enforces against
comes from PayPal, never from the model's own payload.

Versions
--------
Pinned to PayPal's documented agent toolkit setup (Agents SDK + PayPalToolkit +
Configuration/Context, sandbox=True) and the OpenAI Agents SDK's
Agent/Runner shape. If the toolkit changes its exported names, the gate still
holds — ``paypal_guard.actions.resolve`` maps the old and new spellings onto the
same registered verb, which is exactly the failure mode it was written for.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paypal_guard import PayPalGuard, build_protocol, load_policy  # noqa: E402
from paypal_guard.amounts import AmountResult  # noqa: E402

SANDBOX_API = "https://api-m.sandbox.paypal.com"


# ---------------------------------------------------------------------------
# PayPal REST, stdlib only — used for the amount lookup, never for the action
# ---------------------------------------------------------------------------

def access_token(client_id: str, client_secret: str) -> str:
    """Exchange sandbox credentials for an OAuth 2.0 access token."""
    credentials = b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = urllib.request.Request(
        f"{SANDBOX_API}/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)["access_token"]


def order_total(token: str, order_id: str) -> AmountResult | None:
    """Look up what PayPal says an order is worth.

    This is the only place a capture's amount comes from. It is deliberately a
    separate, direct call rather than a call through the gated toolkit: the gate
    needs a trusted price in order to decide, so the price cannot come from the
    thing being gated.

    Returns ``None`` for an order PayPal does not know, which the guard treats as
    unpriceable and therefore refuses.
    """
    request = urllib.request.Request(
        f"{SANDBOX_API}/v2/checkout/orders/{order_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            order = json.load(response)
    except urllib.error.HTTPError as exc:
        print(f"  [resolver] orders.get({order_id}) -> HTTP {exc.code}")
        return None

    totals = [
        unit.get("amount", {}).get("value")
        for unit in order.get("purchase_units", [])
        if unit.get("amount", {}).get("value")
    ]
    if not totals:
        return None

    amount = sum(float(value) for value in totals)
    currency = order["purchase_units"][0]["amount"]["currency_code"].upper()
    return AmountResult(amount, currency, f"orders.get({order_id})")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ask",
        default="List this merchant's open disputes and summarise any risk.",
        help="what to ask the agent",
    )
    parser.add_argument(
        "--policy",
        default=str(ROOT / "policies" / "autopilot.json"),
        help="policy document to enforce",
    )
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
    except ImportError:
        print("Missing dependencies. Run: pip install -r requirements-agents.txt")
        return 1
    load_dotenv(ROOT / ".env")

    client_id = os.environ.get("PAYPAL_CLIENT_ID")
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET")
    merchant_id = os.environ.get("PAYPAL_MERCHANT_ID")
    if not all((client_id, client_secret, merchant_id)):
        print("Set PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET and PAYPAL_MERCHANT_ID in .env")
        return 1

    from agents import Agent, Runner
    from paypal_agent_toolkit.openai.toolkit import PayPalToolkit
    from paypal_agent_toolkit.shared.configuration import Configuration, Context

    from paypal_guard.toolkit import gated_tools

    print("Exchanging sandbox credentials for an access token...")
    token = access_token(client_id, client_secret)

    policy = load_policy(args.policy)
    print()
    print(policy.describe())

    guard = PayPalGuard(build_protocol(policy))
    guard.set_amount_resolver(
        lambda action, params: order_total(token, params.get("id", ""))
    )

    # Enable the full toolkit. The guard, not this list, is what decides: the
    # point is that a broadly-enabled toolkit is still governed. Narrow it for
    # production, but the two layers are not substitutes for each other.
    toolkit = PayPalToolkit(
        client_id=client_id,
        secret=client_secret,
        configuration=Configuration(
            actions={
                "orders": {"create": True, "get": True, "capture": True, "list": True},
                "disputes": {"list": True, "get": True},
                "invoices": {"list": True, "get": True},
            },
            context=Context(sandbox=True),
        ),
    )

    tools = gated_tools(guard, toolkit, merchant_id=merchant_id)
    print(f"\nGated {len(tools)} PayPal tools. Asking the agent:\n")
    print(f"  {args.ask}\n")

    agent = Agent(
        name="paypal-settlement-agent",
        instructions=(
            "You are a settlement agent for a PayPal merchant. Use the PayPal "
            "tools you were given. Never attempt a payment that the tool "
            "response refuses, and never invent a tool. If a payment is held for "
            "approval, tell the user it is awaiting human sign-off and stop."
        ),
        tools=tools,
        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
    )

    result = await Runner.run(agent, args.ask)

    print("\n" + "=" * 74)
    print("AGENT")
    print("=" * 74)
    print(result.final_output)

    print()
    print("=" * 74)
    print("GATE")
    print("=" * 74)
    status = guard.status()
    monitor = status["monitor"]
    print(f"  attempted          : {monitor['action_count']}")
    print(f"  allowed            : {monitor['allowed']}")
    print(f"  blocked            : {monitor['blocked']}")
    print(f"  pending approval   : {monitor['approval_pending']}")
    print(f"  audit events       : {status['audit_entries']}")
    print(f"  chain broken links : {status['audit_broken_links'] or 'none'}")

    if guard.refusals:
        print("\n  refused before any rule was consulted:")
        for refusal in guard.refusals:
            print(f"    {refusal.outcome:<24} {refusal.action}")

    if guard.pending():
        print("\n  waiting on a human:")
        for item in guard.pending():
            print(f"    token={item['token']}  {item['action_type']}  "
                  f"${item['estimated_cost']:.2f}")

    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
