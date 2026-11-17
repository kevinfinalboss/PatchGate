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
| `catalog/profiles/` | Profiles combining agents, apps, allowed app sets, hardening | Referenced by the `Patchgate:Profile` tag |
| `catalog/appsets/` | Reusable app bundles requested with the `Patchgate:AppSets` tag | Pin versions for anything that runs in production |
| `catalog/policies/` | Patch policies: rings, windows, approvers, rollback criteria | |
| `catalog/controls/<os>/` | Hardening controls for the `controls` engine (ADR 0007) | `check` exits 0 when compliant; `remediate` idempotent and safe to re-run; CIS reference required; never lock out SSH/RDP/SSM |
| `scripts/linux/` | `patchgate_install.py` (Python 3.9+ stdlib only) and any helper shell scripts | No third-party imports, no cloud APIs, idempotent; shell helpers `set -euo pipefail` + shellcheck |
| `scripts/windows/` | `Install-PatchgatePlan.ps1` (Windows PowerShell 5.1+) and `PatchgateDsc.psm1` (dsc engine; DSC cmdlets stubbed in tests) | `Stop` on error, PSScriptAnalyzer clean, idempotent; exit `3010` to request reboot; log with `Write-PatchgateLog` (never `Write-Output` inside functions) |
| `catalog/baselines/` | Third-party content for the `ansible` (Linux) and `dsc` (Windows) engines | Every archive/module pinned by sha256 |
| `lib/` | `plan.py` (stdlib only, also runs in Lambda), `schema.py`, `catalog.py` (YAML) | `plan.py` must not import third-party packages |
| `tests/` | Unit tests for `lib/` and catalog validation | No network, no cloud mocks needed |

## Catalog conventions

- Every document has `apiVersion: patchgate.io/v1alpha1`, `kind`, `metadata.name`, `spec`.
- Secrets are referenced as `{{secret:<name>}}`, never inlined.
- Install methods: `choco`, `msi`, `exe`, `package` (apt/dnf auto-detected), `script`. Adding a method touches the schema, both installers and docs.

## Ansible runtime lock

`scripts/linux/ansible-requirements.txt` pins the Ansible runtime used by the `ansible` hardening engine
(Python 3.12 venv, `pip --require-hashes --only-binary :all: --no-deps`). To upgrade:

1. In an `amazonlinux:2023` container with `python3.12`, run `pip-compile --generate-hashes` for
   `ansible-core==<version>` (check `requires_ansible` of every collection used by the baselines).
2. Keep only hashes of `py3-none-any` and `manylinux` x86_64/aarch64 wheels for CPython 3.12/abi3
   (query `https://pypi.org/pypi/<pkg>/<version>/json` to map hashes to file names).
3. Run `make spoke-artifacts` and test a baseline in the lab.

## Baselines (third-party hardening content)

- Pin the archive and **every** collection (including transitive ones) with `sha256`: collections are
  installed with `--no-deps --offline`, so a missing dependency fails loudly instead of downloading.
- Disable rules only through `spec.variables`, each with a comment explaining the exception.
- Container tests validate the mechanics only (no systemd); always verify a baseline on a real instance.
