# Architecture Decision Records

We record significant decisions as ADRs: short documents capturing the context, the decision and its consequences.

## Process

1. Copy [0000-template.md](0000-template.md) to `NNNN-short-title.md` with the next number.
2. Open a PR with status `Proposed`.
3. When merged, set status to `Accepted`. ADRs are immutable afterwards: to change a decision, write a new ADR that supersedes the old one.

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-core-and-providers.md) | Cloud-agnostic core with pluggable providers | Accepted |
| [0002](0002-hub-and-spoke-multi-account.md) | Hub-and-spoke multi-account model | Accepted |
| [0003](0003-native-orchestration-per-provider.md) | Native orchestration per provider | Accepted |
| [0004](0004-event-driven-bootstrap.md) | Event-driven bootstrap instead of user data | Accepted |
| [0005](0005-declarative-agent-catalog.md) | Declarative agent catalog in Git | Accepted |
