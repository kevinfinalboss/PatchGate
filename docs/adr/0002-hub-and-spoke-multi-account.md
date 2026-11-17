# 0002. Hub-and-spoke multi-account model

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Organizations isolate workloads in separate AWS accounts. Patchgate must bootstrap and patch instances in all of them, with one place for approvals, reporting and evidence. It must also work for users with a single account.

## Options considered

1. **Deploy the full platform in every account.** Simple permissions; no central view, approvals and reports scattered, N times the cost and maintenance.
2. **SSM multi-account Automation only.** Native, but no approval gates, weak visibility and limited control over rings.
3. **Hub and spoke.** A central hub runs workflows and assumes a narrowly scoped role in each spoke; spokes forward events to the hub.

## Decision

Option 3.

- A dedicated **hub (tooling) account** in a `Security` OU hosts the event bus, workflows, approval API, inventory and evidence.
- **Spokes** contain only: `Patchgate-Execution` role (trusting specific hub roles), an EventBridge rule forwarding EC2 events, SSM documents, an instance profile and log groups.
- Spoke resources are delivered by a **service-managed CloudFormation StackSet with automatic deployment** to the `Workloads` OU, so new accounts are onboarded automatically. The same resources exist as a Terraform module for single-account and development use.
- **Single-account mode** deploys hub and spoke into one account with no code changes.
- The hub event bus accepts events only from principals in the organization (`aws:PrincipalOrgID`).

## Consequences

- One region per hub: Step Functions SDK integrations run in the hub's region. Multi-region requires a hub per region or a regional fan-out (future ADR).
- Spoke resources are defined twice (Terraform and StackSet) and must be kept in sync; CI should compare them.
- Compromise of the hub is high impact; the hub account needs the strongest controls (see the threat model).
