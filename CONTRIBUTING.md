# Contributing to Patchgate

Thanks for your interest in Patchgate! This guide explains how to propose changes.

## Before you start

- Read the [architecture](docs/architecture.md) and the [roadmap](docs/roadmap.md).
- For anything larger than a small fix, **open an issue first** so we can agree on the approach.
- Security issues follow [SECURITY.md](SECURITY.md), never public issues.
- By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Required tools:

- Python 3.12 (`make setup` creates `.venv` and installs the dev dependencies)
- Terraform ≥ 1.9, `tflint`, `checkov` 3.4.5 (same version as CI; `make security` uses Docker if available)
- `shellcheck` and PowerShell 7 with `PSScriptAnalyzer`
- `make`

Run every check locally before opening a PR:

```bash
make check
```

## Where does my change go?

| Change | Location |
|---|---|
| New agent template | `core/catalog/agents/` |
| New profile or patch policy example | `core/catalog/profiles/`, `core/catalog/policies/` |
| Script that runs inside the VM | `core/scripts/linux/` or `core/scripts/windows/` |
| Hardening rule | `core/hardening/` |
| Ring/approval/compliance logic | `core/lib/` |
| Anything that calls a cloud API | `providers/<cloud>/` |
| New cloud provider (not planned) | `providers/<cloud>/` + ADR (see [providers/README.md](providers/README.md)) |

Golden rule: **`core/` never knows which cloud it runs on.**

## Pull requests

1. Fork and create a branch from `main` (`feat/short-description`, `fix/...`, `docs/...`).
2. Use [Conventional Commits](https://www.conventionalcommits.org/): `feat(core): add msi install method`.
   Scopes: `core`, `aws`, `catalog`, `docs`, `ci`.
3. Add or update tests and docs.
4. Add an entry to [CHANGELOG.md](CHANGELOG.md) under `Unreleased` for user-visible changes.
5. Fill in the PR template and make sure CI is green.

### Architectural decisions

Changes to schemas, the provider contract, event types or the overall architecture need an ADR. Copy [docs/adr/0000-template.md](docs/adr/0000-template.md) to the next number and include it in your PR.

### Adding an agent to the catalog

- One YAML file per agent, named after `metadata.name`.
- Downloaded artifacts must have a `sha256`.
- Licenses, tokens and IDs are referenced as `{{secret:<name>}}`, never committed.
- Include a `validate` command that proves the agent is running.
- Test on at least one Linux and/or Windows image you declare in `platforms`.

## Testing against real cloud accounts

Unit and contract tests never touch real accounts. If you deploy to your own account to test:

- Use the `lab` module and a dedicated sandbox account.
- Set an AWS Budget alert before deploying.
- Destroy the lab when you finish.

## AI-assisted contributions

AI coding agents are welcome. They should follow [AGENTS.md](AGENTS.md), and you remain responsible for reviewing and understanding every line you submit.
