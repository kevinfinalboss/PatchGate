# 0005. Declarative agent catalog in Git

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Every organization installs different agents (EDR, antivirus, monitoring, backup) in different ways. Hardcoding agents in Patchgate would make it useful only for one setup. Users need to declare what to install and how, safely.

## Options considered

1. **Hardcoded agents with feature flags.** Simple; not extensible.
2. **Catalog in a database with a UI.** Flexible; needs an app, auth and a backup strategy before delivering value.
3. **Declarative YAML catalog in Git (GitOps)**, validated by JSON Schema in CI and published by the provider.

## Decision

Option 3. Three document kinds, all `apiVersion: patchgate.io/v1alpha1`:

- **Agent:** supported platforms, install method (`choco`, `msi`, `exe`, `package`, `script`), source, `sha256`, arguments, validation command.
- **Profile:** list of agents, Chocolatey packages, hardening level and ring; selected by the `Patchgate:Profile` tag.
- **PatchPolicy:** rings, maintenance windows, soak time, approvers, rollback criteria.

Secrets are referenced as `{{secret:<name>}}` and resolved at runtime from the provider's secret store.

## Consequences

- Changes are reviewed via pull requests and fully versioned.
- Generic installers in `core/scripts/` must support every install method and verify checksums.
- Schema evolution needs versioning (`v1alpha1` → `v1beta1` → `v1`) and migration notes.
- A UI can be added later on top of the same schemas.
