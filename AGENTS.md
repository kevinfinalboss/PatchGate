# AGENTS.md

Instructions for AI coding agents working on Patchgate. Humans: see [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## Project in one paragraph

Patchgate hardens, bootstraps and patches Linux/Windows fleets across cloud accounts. The code is split into a **cloud-agnostic `core/`** and **`providers/<cloud>/`** that implement the [provider contract](providers/README.md). AWS is the only provider today. Current status: **Phase 0** (see [docs/roadmap.md](docs/roadmap.md)). Many folders are still empty; do not invent code that a phase has not asked for.

## Nested instructions

Before editing files under these folders, read their own AGENTS.md (closest file wins):

- [core/AGENTS.md](core/AGENTS.md)
- [providers/aws/AGENTS.md](providers/aws/AGENTS.md)

## Hard rules

1. **No cloud SDKs in `core/`.** `core/` must never import `boto3`, `azure-*`, or reference AWS/Azure resource names. If you need cloud behaviour, add it to the provider contract and implement it in `providers/<cloud>/`.
2. **Never commit secrets**, account IDs, license keys or agent CIDs. Secrets are referenced as `{{secret:<name>}}` in the catalog and resolved at runtime by the provider.
3. **Least privilege.** New IAM statements must name specific actions and scope resources/conditions. `"Action": "*"` or `"Resource": "*"` on write actions is rejected in review.
4. **Every install must be verifiable.** Catalog entries that download artifacts must carry a `sha256`. Scripts must fail closed on checksum mismatch.
5. **In-VM scripts must be idempotent.** Running them twice must produce the same result and exit 0.
6. **Values interpolated into scripts must be validated** (allowed-pattern/regex) before reaching a shell or PowerShell. Never build commands by concatenating unvalidated tags or user input.
7. **Do not change public contracts silently.** Changing a JSON Schema in `core/schemas/`, the provider contract or the CloudEvents types requires an ADR in `docs/adr/` and a CHANGELOG entry.
8. **Destructive cloud commands** (`terraform apply/destroy`, `aws ... delete-*`) must never be run by an agent without explicit human approval.

## Tooling and commands

> These commands become available as Phase 0 tooling lands. If a command here does not work, fix this file in the same PR.

| Task | Command |
|---|---|
| Run all checks | `make check` |
| Python lint / format | `ruff check .` / `ruff format .` |
| Python types | `mypy core/lib providers/aws/functions` |
| Python tests | `pytest` (AWS calls mocked with `moto`; tests never touch real accounts) |
| Terraform format | `terraform fmt -recursive providers/aws/terraform` |
| Terraform lint | `tflint --recursive` |
| IaC security scan | `checkov -d providers/aws` |
| Catalog validation | `make validate-catalog` (JSON Schema against `core/catalog/**`) |
| PowerShell lint | `Invoke-ScriptAnalyzer -Recurse core/scripts/windows` |
| Catalog CLI | `python -m patchgate validate` / `compile <profile>` / `export` (seed); `make seed` after catalog changes |
| Config/instance CLI | `patchgate-cli --help` (DynamoDB configuration store, see providers/aws/README.md) |
| Lambda layer | `make layer` (lock regenerated inside `public.ecr.aws/lambda/python:3.12`) |
| Windows installer tests | need `pwsh`; set `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1` if libicu is missing |

## Stack and conventions

- **Python 3.12**, type hints everywhere, `ruff` for lint and format, `pytest` for tests.
- **Terraform ≥ 1.9**, AWS provider pinned in `versions.tf`. One module per concern; no logic in `live/` roots beyond wiring.
- **Shell:** `bash` with `set -euo pipefail`. **PowerShell:** `$ErrorActionPreference = 'Stop'`.
- **Naming:** tags and resource prefixes use `Patchgate:` (tags) and `patchgate-` (resources).
- **Language:** all code, comments, docs and commit messages in English.
- **Single-account mode must keep working.** Anything multi-account must also deploy with hub and spoke in the same account.

## Tests

- Add or update tests for every behaviour you change in `core/lib` or `providers/*/functions`.
- Pure logic (rings, approval rules, compliance math) is tested in `core/tests/` with no mocks of cloud APIs.
- Provider code is tested against the contract with mocked SDKs in `providers/<cloud>/tests/`.

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/): `feat(core): ...`, `fix(aws): ...`, `docs(adr): ...`. Scopes: `core`, `aws`, `catalog`, `docs`, `ci`.
- One logical change per PR. Run `make check` before opening it.
- Architectural decisions go in a new ADR (`docs/adr/NNNN-title.md`, from the template).
- Update [CHANGELOG.md](CHANGELOG.md) under `Unreleased` for user-visible changes.

## Anti-patterns (do not do this)

- Putting orchestration logic (rings, approvals) in Step Functions JSON when it belongs in `core/lib` as a testable function.
- Using EC2 `user_data` for bootstrap — bootstrap is event-driven and auditable (see ADR 0004).
- Hardcoding agent names or package lists in provider code — they live in the catalog.
- Adding a new cloud service without updating `docs/architecture.md` and the cost notes in the roadmap.
