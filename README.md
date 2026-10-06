# paypal-agentsafe

**An AI agent can call PayPal. This makes sure it can only do what you actually
authorized — and can prove afterwards exactly what it tried to do.**

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/).

PayPal ships an official agent toolkit that lets a model create orders, send
invoices, and capture payments. That is a spending capability with no ceiling,
handed to something that can be talked into using it differently than you meant.

This project puts a policy gate in front of it. The agent still chooses what to
ask for. The policy decides whether it happens, in code, on every call, with no
way for the model to talk past it.

```
agent wants to capture $240  ->  gate  ->  policy  ->  human  ->  PayPal
                                 |           |          |
                          closed verb   deny-by-    approval
                          known price   default     queue
```

## Try it in 30 seconds, no PayPal account needed

```bash
git clone https://github.com/TheDub-lab/paypal-agentsafe
cd paypal-agentsafe
python examples/offline_demo.py
```

Eleven scenarios, including a capture held for a human, an invented verb, an
unpriceable capture, a well-formed request against the wrong merchant, and a
kill switch. No dependencies, no network, no credentials — the guard is stdlib
only.

```bash
pip install -e ".[dev]" && pytest      # 113 tests
```

## The part that is actually different

Most agent guardrails are prompts. A system prompt says *please don't refund
more than $50*. This is not that. Three refusals happen in infrastructure, and
each one is a decision the model cannot influence:

**1. Unregistered verbs are refused.** The agent's action vocabulary is closed:
24 named PayPal operations. If the model reaches for something else — a refund
verb that does not exist, a sub-agent it was never granted, a tool invented in
the moment — it is refused before any rule is consulted. Scope is an allowlist,
so a verb with no rule behind it can only ever be denied.

**2. Money that cannot be priced is refused.** Every cap in this project is
expressed in dollars, which requires knowing what the action costs. So amounts
are read from the documented shape of each PayPal operation, and when the shape
is not recognised the extraction **fails closed**.

The hard case is `orders.capture`, which carries only an order id — the amount
is not in the request at all. The honest answer is not to guess. The guard
requires an amount resolver that fetched the total from PayPal first:

```python
guard.set_amount_resolver(lambda action, params: order_total_from_paypal(params["id"]))
```

Capture without a known price is blocked. *An unpriced capture is an unbounded
capture.* Defaulting an unknown amount to `0.00` would wave every spend past
every cap, and that one line is the difference between a guard and a decoration.

**3. A policy that is broader than you meant does not boot.** A rule that grants
a target without binding an HTTP method, a parameter schema, or a spend cap is
rejected at load time. So is one that grants an operation PayPal does not have.
The agent does not start with a more permissive policy than the one you wrote.

## What the gate enforces

| Control | What it stops |
| --- | --- |
| **Closed vocabulary** | The model inventing a capability it was never given |
| **Deny-by-default scope** | Acting on a merchant, order, or invoice you did not name |
| **Method + param binding** | A "read" rule quietly permitting a write, or a safe endpoint taking `?confirm=true` |
| **Per-action spend cap** | One oversized payment inside a healthy budget |
| **Session budget** | Many small payments draining an account |
| **Approval threshold** | Anything significant waiting for a human, before it moves |
| **Kill switch** | Everything, immediately, from one call — overrides pending approvals |
| **Revocation** | Permanent loss of authority that unfreezing cannot undo |
| **Hash-chained audit** | An answer to "what did it try to do, and what stopped it" |

The approval queue carries the amount, so the human deciding sees the number
that triggered the decision. Approving one intent does not approve a different
order or a different amount, and the whitelisted retry expires in five minutes.

## Use it with the real PayPal toolkit

```python
from paypal_guard import PayPalGuard, build_protocol, load_policy
from paypal_guard.toolkit import build_toolkit, gated_tools

policy = load_policy("policy.example.json")
print(policy.describe())          # read your own authorisation surface

guard = PayPalGuard(build_protocol(policy))
guard.set_amount_resolver(price_from_paypal)   # required for captures

toolkit = build_toolkit(actions={"orders": {"create": True, "capture": True}})
tools = gated_tools(guard, toolkit, merchant_id="ACME-MERCH-4417")
# hand `tools` to your agent framework
```

Every tool the model sees is a wrapper whose first step is `guard.call`. A
refusal comes back to the model as ordinary tool output, so the agent explains
the refusal rather than failing silently.

Tool-name changes do not become permission changes: `orders.capture`,
`orders_capture`, `captureOrder`, and `capture_order` all resolve to the same
registered verb, so an upstream rename cannot silently widen what the agent can
reach.

### Run it against the sandbox

```bash
pip install -r requirements-agents.txt
cp .env.example .env          # sandbox credentials from developer.paypal.com
python examples/agentsdk_agent.py --ask "list this merchant's open disputes"
```

## The policy is the whole authorisation surface

```json
{
  "agent_id": "paypal-settlement-agent",
  "user_id": "merchant:ACME-MERCH-4417",
  "budget_limit": 500.0,
  "approval_threshold_cost": 100.0,
  "allow": [
    {
      "action": "paypal_order_capture",
      "targets": ["paypal/merchant/ACME-MERCH-4417/order/*"],
      "match": "glob",
      "methods": ["POST"],
      "max_cost": 300.0,
      "requires_approval": true,
      "forbidden_targets": ["*unverified*"],
      "params": {
        "required": ["id"],
        "additional_properties": false,
        "properties": { "id": { "type": "string", "pattern": "^[A-Z0-9]{6,32}$" } }
      }
    }
  ]
}
```

Two example policies ship, and the difference between them is the point:

- **`policy.example.json`** — a bookkeeper. It cannot capture at all.
- **`policies/autopilot.json`** — a settlement agent that can capture, within a
  per-order cap, with human sign-off above the threshold.

Both compile with zero linter findings. Change one and it will refuse to load.

## Audit

Every attempt is recorded — allowed, blocked, held, or refused by the gate
itself — in a hash-chained log. Each entry stores the hash of the previous entry
*and* a hash of its own contents, and verification recomputes both, so an
in-place edit to a recorded amount is detected rather than merely improbable.

The offline demo ends by editing an audited amount and showing the chain report
the break. This check was added because the first version of the chain verified
only the links, which an in-place edit leaves completely intact.

## Layout

```
src/paypal_guard/
  governance/     the governance engine (vendored, stdlib only) — see NOTICE
  actions.py      the closed vocabulary of 24 PayPal operations
  amounts.py      fail-closed USD extraction from PayPal payloads
  policy.py       policy document -> ScopeRules, with the deployment-time linter
  guard.py        the gate; the only path to the transport
  toolkit.py      wrapping @paypal/agent-toolkit
examples/
  offline_demo.py    the whole gate, no credentials
  agentsdk_agent.py  the same gate, live, on the sandbox
policies/         two example authorisation surfaces
docs/             threat model
```

## Attribution, and two bugs this found

The governance engine in `src/paypal_guard/governance/` is vendored from
[AgentCover Sovereign](https://github.com/TheDub-lab/agentcover-sovereign) (MIT),
our earlier submission for the Nebius x NVIDIA hackathon. See `NOTICE` for its
provenance and the full list of changes. It is vendored deliberately, and only
in its stdlib-only form, so this repository stands alone: one clone, one
`pip install`, one demo, no second project and no package index.

Integrating it against PayPal surfaced two defects in it, both found by running
the demo rather than by reading the code:

1. **`verify_integrity` did not actually detect tampering.** It checked that
   each entry's `prev_hash` matched its predecessor. Rewriting an amount in
   place left every link and every stored hash intact, so the tamper-evidence
   claim did not hold. It now recomputes each entry's hash.
2. **An approved action was charged twice.** `decide_approval` charged the
   session budget and recorded the action through the monitor, and the retry
   that followed did both again — so an approved $240 capture consumed $480 of a
   $500 budget and was then refused for money never spent.

Both are fixed here. Also added: `check_scope_only`, so the gate can answer the
scope question *before* firing a priced PayPal lookup for a merchant the agent
was never granted, plus the read-only `frozen` and `revoked` properties so the
guard can observe a kill switch without calling `execute` and issuing PayPal
reads during a fraud freeze. The full list is in
[`NOTICE`](NOTICE).

## Limitations, stated plainly

- **The amount resolver is trusted.** The gate enforces against whatever the
  resolver returns. A resolver that lies defeats the cap, so in production it
  should be a narrow, separately-audited function over PayPal's own response.
- **The audit trail is in-memory.** It is tamper-evident, not durable. A real
  deployment persists entries and anchors the head hash somewhere the agent
  cannot write, which is what makes the record usable to a third party.
- **Bindings are simulated.** No on-chain identity layer here; this submission is
  scoped to the PayPal surface.
- **The merchant Cart API is not implemented.** PayPal's Store Sync partner
  program gates access behind an approval process. `paypal_guard.toolkit` is the
  agent-side integration and needs no approval.
- **The toolkit wrapper is written against PayPal's documented export shapes**
  for four frameworks. If a shape differs, `wrap_tool` raises rather than
  silently passing a tool through ungated.

## License

MIT. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

PayPal, PayPal.com, and the PayPal logo are trademarks of PayPal, Inc. This
project is not endorsed by or affiliated with PayPal, Inc.