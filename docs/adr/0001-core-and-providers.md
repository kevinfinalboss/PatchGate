# 0001. Cloud-agnostic core with pluggable providers

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Patchgate starts on AWS, but fleets increasingly span AWS and Azure. Rewriting the platform per cloud would duplicate the parts that are identical everywhere: what to install, how to harden, how rings and approvals behave. At the same time, a large generic "cloud abstraction layer" designed before a second cloud exists usually ends up wrong.

## Options considered

1. **AWS-only design.** Fastest start; a second cloud means a rewrite.
2. **Full abstraction from day one** (generic orchestrator, generic IaC). Maximum portability on paper; heavy, speculative, slows every feature.
3. **Thin core + providers.** Share only what is cloud-independent today; keep cloud specifics in providers behind a small contract.

## Decision

Option 3.

- `core/` holds schemas, catalog, in-VM scripts, hardening content and pure Python logic (rings, approvals, compliance, CloudEvents models). It must not import cloud SDKs.
- `providers/<cloud>/` holds IaC, orchestration, identity and adapters implementing the [provider contract](../../providers/README.md).
- The contract grows only when a second provider proves an operation is common.

## Consequences

- Adding Azure means adding `providers/azure/` without touching `core/`; needing to touch it reveals a contract gap.
- Some logic is deliberately duplicated across providers (for example orchestration), see [ADR 0003](0003-native-orchestration-per-provider.md).
- CI must enforce the boundary (no cloud SDK imports in `core/`).
