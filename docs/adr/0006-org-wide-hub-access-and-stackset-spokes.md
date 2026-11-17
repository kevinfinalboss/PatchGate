# 0006. Organization-wide hub access and StackSet-managed spokes

- **Status:** Accepted
- **Date:** 2026-11-17
- **Refines:** [0002](0002-hub-and-spoke-multi-account.md)

## Context

ADR 0002 delivers spokes through a StackSet that auto-deploys to the Workloads OU. The first
implementation, however, listed spoke account IDs explicitly in the hub's IAM policy, so every new
account still required a hub change, defeating automatic onboarding. Spokes were also applied with
Terraform per account, duplicating the StackSet.

## Options considered

1. **Keep an explicit account list in the hub.** Tightest policy; onboarding needs a manual change.
2. **Wildcard account (`arn:aws:iam::*:role/Patchgate-Execution`).** Zero-touch; relies only on the
   spoke trust policy.
3. **Wildcard account constrained by `aws:ResourceOrgID`.** Zero-touch and limited to roles owned by
   accounts in the organization.

## Decision

Option 3, verified against live accounts (AssumeRole succeeds for member accounts with the condition).

- Spokes in multi-account mode are owned by the `patchgate-spoke` StackSet (service-managed,
  auto-deploy, stacks removed when an account leaves the OU). Terraform-applied spokes are only used in
  single-account mode.
- SSM documents and the CloudFormation template are generated from `core/` by
  `providers/aws/stacksets/generate.py`; both deployment paths consume the same documents. CI fails
  on stale artifacts and parity tests compare resource names and permissions.
- Single-account mode (`organization_id = null`) restricts the hub to its own account and drops the
  organization bus policy.

## Consequences

- New accounts in the Workloads OU are protected without touching the hub (demonstrated by moving an
  account out of and back into the OU).
- Defense in depth: the hub can only reach org accounts, and each spoke trusts only the two named hub roles.
- Moving an account out of the Workloads OU removes its spoke (and the budget kill switch no longer covers it).
