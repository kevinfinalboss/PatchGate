# AGENTS.md — core/

Cloud-agnostic part of Patchgate. Root rules in [/AGENTS.md](../AGENTS.md) still apply.

## Boundaries

- Allowed dependencies in `core/lib`: Python standard library, `pydantic`, `jsonschema`, `pyyaml`, `cloudevents`. Anything else needs an ADR.
- **Forbidden:** `boto3`, `botocore`, `azure-*`, ARNs, resource IDs, region names.
- `core/scripts/` run **inside the VM** and must not call cloud APIs or assume which cloud they run on.

## Folders

| Path | Contents | Rules |
|---|---|---|
| `schemas/` | JSON Schemas for agent, profile and patch-policy | Breaking change ⇒ bump `apiVersion` + ADR |
| `catalog/agents/` | One YAML file per installable agent | File name = `metadata.name`; `sha256` required for downloads |
| `catalog/profiles/` | Profiles combining agents, Chocolatey packages, hardening level | Referenced by the `Patchgate:Profile` tag |
| `catalog/policies/` | Patch policies: rings, windows, approvers, rollback criteria | |
| `scripts/linux/` | Bash installers, health checks | `set -euo pipefail`, shellcheck clean, idempotent |
| `scripts/windows/` | PowerShell installers, Chocolatey, health checks | `Stop` on error, PSScriptAnalyzer clean, idempotent; exit `3010` to request reboot |
| `hardening/ansible/` | Linux CIS hardening | Levels: `cis-level1`, `cis-level2` |
| `hardening/dsc/` | Windows CIS hardening (DSC) | Same levels as Linux |
| `lib/` | Python: ring selection, approval rules, compliance math, CloudEvents models | Pure functions, 100% unit-testable |
| `tests/` | Unit tests for `lib/` and catalog validation | No network, no cloud mocks needed |

## Catalog conventions

- Every document has `apiVersion: patchgate.io/v1alpha1`, `kind`, `metadata.name`, `spec`.
- Secrets are referenced as `{{secret:<name>}}`, never inlined.
- Install methods: `choco`, `msi`, `exe`, `package` (apt/dnf auto-detected), `script`. Adding a method touches the schema, both installers and docs.
