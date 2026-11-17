# 0003. Native orchestration per provider

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Bootstrap and patching are long-running, multi-step workflows with waits, retries, human approval and rollback. They need durable state and a clear audit trail.

## Options considered

1. **SSM Maintenance Windows / State Manager alone.** Cheap and native; no human approval step, little branching, weak visibility.
2. **Cloud-agnostic orchestrator (for example Temporal).** One workflow codebase for every cloud; requires running and securing a cluster, high cost and complexity for this stage.
3. **Native serverless orchestrator per provider** (Step Functions on AWS, Durable Functions on Azure) driven by policies from `core/`.

## Decision

Option 3. On AWS, workflows are Step Functions state machines using:

- AWS SDK integrations with the `Credentials` field to act in spoke accounts;
- `waitForTaskToken` for email approvals;
- Lambda only where logic is needed — and that logic lives in `core/lib` so it is shared and unit-tested.

## Consequences

- Workflow definitions are duplicated per provider; business rules are not (they live in `core/lib` and `core/catalog/policies`).
- Execution history in Step Functions provides a visual audit trail for free.
- Revisit option 2 if more than two providers exist or workflows diverge.
