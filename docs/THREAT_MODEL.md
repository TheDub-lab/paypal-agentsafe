# Threat model

What this gate is defending against, and what it is not. Written before the code
was finished so the claims in the README have something to be checked against.

## The asset

Money in a PayPal merchant account, and the authority to move it. Secondary
assets: the merchant's order and dispute data, and the credibility of records
used for reconciliation or disputes.

## The adversary

Not a hacker. The realistic threat is a well-meaning or subtly-influenced agent
acting outside the operator's intent. Concretely:

| # | Threat | Where it is stopped |
| --- | --- | --- |
| T1 | **Prompt injection.** Untrusted text in an invoice, order memo, dispute description, or email tells the agent to issue a refund or pay a different amount. | Scope allowlist + closed vocabulary. The injected text can change what the model *asks for*; it cannot change what the policy *permits*. |
| T2 | **Capability invention.** The model calls a tool or verb that was never granted, because the framework exposed it or the model produced it. | Closed vocabulary, checked before any rule. |
| T3 | **Scope creep across accounts.** A request that is well-formed but names a different merchant's order. | Deny-by-default target allowlist, checked before the amount resolver runs. |
| T4 | **Amount obfuscation.** The model supplies a payload whose amount cannot be parsed, so the cost reads as zero and sails past every cap. | Fail-closed amount extraction. Unknown amount is a refusal, never `0.00`. |
| T5 | **Currency confusion.** Amounts in two currencies, so no single-currency budget applies. | Mixed-currency purchase units are refused rather than summed. |
| T6 | **Death by a thousand payments.** Individually trivial amounts, collectively the account. | Session budget on top of the per-action cap. The cap catches size, the budget catches volume. |
| T7 | **The cap is the target.** One payment just under the per-action cap, or just under the approval threshold. | Thresholds and caps are separate controls. An amount that clears both still has to clear the human. |
| T8 | **Approval laundering.** A human approves order A at $240; the agent then captures order B, or A at a different amount. | Approval whitelists one `(target, cost-cents)` pair for five minutes. A different order or amount needs its own approval. |
| T9 | **Kill-switch bypass.** Action continues after the operator freezes the account. | `check_scope_only` runs before the resolver, but `execute` checks the kill switch before anything else, so a frozen protocol never prices or executes. Freezing also overrides already-approved intents. |
| T10 | **Credential leakage into the audit log.** A payload carries a secret, which is then written to a durable, widely-readable log. | Credential-shaped keys are redacted before validation, before audit, and before the transport. |
| T11 | **Undetectable history.** The record is edited after the fact to hide what happened. | Per-entry content hashes, recomputed on verification. |
| T12 | **Policy drift.** An operator edits a policy at deploy time and accidentally grants far more than intended, or grants a verb that does not exist so the rule silently never matches. | Deployment-time linter and vocabulary check; both refuse to compile. |

## Trust boundaries

```
   untrusted input            policy document (operator-authored, linted)
   invoice text, memos   |     |
   dispute bodies             v
        |               compiled ScopeRules
        v                    |
   +---------+          +----+----+
   |  model  |  ------> |   gate   |   the only component that decides
   +---------+          +----+----+
                              |
                     allowed  |  blocked / pending / refused
                              v
                        PayPal API
```

Everything left of the gate is assumed to be adversarial or unreliable,
including the model's own account of what it is doing. The policy document is
trusted but *verified*: it is linted at load time rather than assumed correct.

The amount resolver is trusted code inside the gate, and is the one component
this project does not police. T4's guarantee is only as strong as it.

## Out of scope

These are real problems this project does not solve, listed so the boundary is
not mistaken for the edge of the work:

- **Compromise of the host.** An attacker with code execution on the box that
  runs the agent can edit the policy before it compiles. The linter catches
  accidents, not an attacker with write access.
- **A malicious operator.** Anyone who can write the policy can authorise
  anything. The gate enforces the policy faithfully; it does not judge whether
  the policy is wise.
- **On-chain identity and accountability.** Binding, revocation, and third-party
  verification of *who* authorised a payment are out of scope here.
- **Durability and non-repudiation.** The audit trail is in-memory. It is
  tamper-evident within a process lifetime and no further.
- **PayPal-side fraud controls.** This gate decides whether the *agent* may act.
  It has no bearing on card testing, account takeover, or a compromised PayPal
  session token.
- **Multi-agent delegation.** Sub-agent spawning is not implemented.

## Design decisions that follow from this

**Why an amount resolver rather than trusting the payload.** `orders.capture`
carries only an order id. There is no amount to read. Either the gate accepts an
unpriced money movement, or it requires someone to look the price up first. The
second option is the only one that keeps a cap meaningful, which is why
`PayPalGuard` refuses to proceed without one.

**Why scope is checked before pricing.** A priced lookup is a real API call.
Running it before establishing that the action is in scope would mean querying
PayPal about orders belonging to a merchant the agent was never granted — a
small authorisation failure inside the component meant to prevent large ones.
This is why `check_scope_only` exists as a separate, side-effect-free entry
point.

**Why refusals that happen before the protocol still get audited.** An
unregistered verb and an unpriceable amount are not rule evaluations, so they
never reach `SafetyProtocol.execute`. They are still written to the same
hash-chained trail and counted in the same blocked-action counter, because an
attempt that was refused is precisely the evidence an operator needs when the
agent starts probing for a way around the gate.