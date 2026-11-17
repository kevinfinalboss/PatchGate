# 0004. Event-driven bootstrap instead of user data

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

New instances must receive agents, packages and a hardening baseline right after launch, regardless of who launched them (console, IaC, Auto Scaling) and without relying on teams remembering to add scripts.

## Options considered

1. **EC2 user data.** Runs at first boot; depends on every launch template including it, no central visibility or retries, secrets tend to leak into launch templates.
2. **State Manager association targeting tags.** Native and self-healing; slower to react, harder to report per-instance bootstrap outcome and to notify.
3. **Event-driven:** an EC2 `running` event triggers a hub workflow that runs SSM documents on the instance.

## Decision

Option 3 for bootstrap. Option 2 is used later for **drift correction** (Phase 3), complementing it.

The workflow skips instances that are not tagged `Patchgate:Managed=true` or already bootstrapped, waits (bounded) for the SSM agent to come online, runs the profile, records the result as a tag and emits a CloudEvent.

## Consequences

- Works for any launch method, including instances created outside IaC.
- Every bootstrap has an execution history, retries and notifications.
- Requires the SSM agent and connectivity to SSM endpoints (included in common AMIs and the golden image).
- Stop/start emits `running` again; workflows must be idempotent and skip completed instances.
