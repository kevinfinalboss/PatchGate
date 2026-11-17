# 0008. Configuration store in DynamoDB, managed like AWS Instance Scheduler

- **Status:** Accepted
- **Date:** 2026-11-17
- **Supersedes:** the storage part of [0005](0005-declarative-agent-catalog.md) (the document model is unchanged)

## Context

ADR 0005 kept the catalog in Git and compiled it into the resolver package. That suits platform teams,
but most users (application teams, operators) want to assign and change software without pull
requests and redeploys. With app sets, plans also depend on per-instance tags, so they can no longer
be precompiled.

## Options considered

1. **Git only (status quo).** Reviews and history for free; every change needs a PR and a deploy.
2. **Compiled catalog in S3, published by CI.** No redeploys; still Git-only editing.
3. **DynamoDB as the runtime source of truth**, managed with a CLI, the console or IaC, like the
   AWS Instance Scheduler: configuration in a table, instances only carry tags.

## Decision

Option 3.

- `patchgate-config` (partition `kind`, sort `name`, on-demand, PITR, streams, deletion protection)
  stores one item per document with `version`, `updatedBy`, `updatedAt` and `managedBy`.
- Built-in content is seeded on deploy (`managedBy = patchgate`) and owned by releases; users
  customise by copying (`patchgate-cli copy`). User items are never touched by deploys.
- `patchgate-cli` validates schemas and references before every write, uses optimistic locking and
  refuses to delete referenced documents. Git stays available: YAML files can be applied with
  `patchgate-cli apply -f`.
- The resolver loads the profile and the instance's app sets at run time, validates every document
  and **refuses** invalid or inconsistent configuration (protects against direct console edits).
- DynamoDB Streams feed `patchgate-config-history` (previous and new document, author) for audit and
  `patchgate-cli rollback`; invalid writes raise an alert.

## Consequences

- Configuration changes apply on the next bootstrap, tag change or drift run without redeploying.
- No pull-request review by default: approval of risky changes is part of Phase 4b (rollouts).
- The plan library runs in Lambda (`patchgate-core` layer with hash-pinned dependencies).
