# 0010. Approval engine: rings, approver groups and per-unit releases

- **Status:** Accepted
- **Date:** 2026-11-18

## Context

Profiles and app sets live in the configuration store ([ADR 0008](0008-configuration-store-in-dynamodb.md)).
Two problems remained after Phase 4b:

- A change to a profile or app set does not reach running instances: they re-plan only on launch
  or when a `Patchgate:*` tag changes. Fixing that naively would push every change to the whole
  fleet at once, with no approval.
- OS patching (Phase 4c-2) needs the same thing: changes applied ring by ring, with an approval
  gate before production, health checks and a way back.

Approval must be possible per account and, where teams share an account, per instance.

## Options considered

1. **One approval per change for the whole fleet.** Simple, but one team cannot approve its own
   instances without approving everybody's.
2. **Approval per instance.** Precise but unusable: one email per instance.
3. **Approval per unit = ring + approver group.** Each group approves only its own instances in a
   ring; a rejection leaves just that group on the previous version.

For the version each instance receives:

- **a.** Copy approved documents into separate "released" items. Duplicates documents and history.
- **b.** Record, per document, which *revision* each unit received and read that revision from the
  history table that already stores every version.

For approval links: SNS email (same message for every subscriber, so links cannot identify the
approver) or SES (one message per approver).

## Decision

Options **3** and **b**, with SES.

- **Gated kinds:** `AppSet` and `Profile` (configurable in the `RolloutPolicy`). Other kinds keep
  today's behaviour.
- **Rings come from the account**, stored in the configuration table (`Account`, provider-side,
  never in Git because it holds account IDs). Unregistered accounts use the last ring. A
  `Patchgate:Ring` tag is honoured only where the account allows it (single-account mode).
  The profile `ring` field is deprecated and ignored.
- **Approver groups** (`ApproverGroup`: members, `minApprovals`) are assigned per account and may
  be overridden per instance with `Patchgate:Approvers`, limited to the groups the account allows.
  A disallowed tag is ignored and reported. Any member's rejection rejects; approvals count once
  per member.
- **Releases:** each gated document records `{unit: revision}` plus `"*"` (the revision for every
  other unit) in a separate `patchgate-releases` table, so configuration writes never overwrite
  them and the history only holds real changes. A *revision* is the sha256 of the stored
  document, not its version number: built-in documents are re-seeded by every deploy with
  version 1, so only the content identifies them. Documents without releases are served as
  stored (new documents: no instance uses them yet). Before a gated document changes,
  `patchgate-cli` pins the current revision as `"*"`; the audit function does the same for
  changes made elsewhere (console, re-seeded built-ins), with a window of seconds in which a
  bootstrap could still see the new revision. A rollout releases unit by unit; when every ring
  had instances and every unit took the revision, it becomes `"*"`. Otherwise `"*"` stays, so
  instances nobody approved for, including future ones, never receive it.
- **Rollout policy** (`RolloutPolicy`): rings in order, `approval: none | required`, `soak`,
  `approvalTimeout` (unanswered = rejected) and `onFailure: revert | stop`. `revert` releases
  the previous version to the failed ring again; the rollout always stops on failure.
- **Applying a ring** re-runs the existing bootstrap workflow per instance (lock, SSM, evidence);
  the ring is healthy when every run succeeds.
- **Approval links** are HMAC-signed, expiring tokens naming the request and the approver. They
  open a confirmation page; only its POST decides, so mail scanners that open links decide
  nothing. Single use is a conditional write in the approvals table, which also keeps who
  decided what and when. The Step Functions task token never leaves the hub.
- Ring, group, release and quorum rules are pure functions in `core/lib/patchgate/rollout.py`
  and `approval.py`; the provider only orchestrates.

## Consequences

- Changes to profiles and app sets finally reach running instances, in a controlled way.
- A version stays pending for a unit until that unit approves; new instances in rings with no
  instances during the rollout keep the previous version until a later rollout covers them.
- First public endpoint of the project (approval API): the threat model must cover it. Approval
  is "possession of the email" — strong authentication (SSO) is out of scope for now.
- New services: Amazon SES and an HTTP API; architecture and cost notes are updated with them.
- Phase 4c-2 (OS patching) reuses the engine; only the "apply" step and the health checks change.
