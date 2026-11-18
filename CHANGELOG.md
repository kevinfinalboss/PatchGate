# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Agent catalog (`v1alpha1` schemas), templates for CloudWatch Agent, ClamAV and Microsoft Defender, and profiles `base-linux`, `base-windows`, `web-windows`.
- `patchgate` CLI to validate the catalog and compile compressed install plans.
- In-VM installers for Linux (Python stdlib) and Windows (PowerShell 5.1+) with checksum verification, Chocolatey bootstrapped from a pinned package and idempotent re-runs.
- AWS provider: hub (event bus, bootstrap workflow, resolver Lambda, notifications), spoke (execution role, event forwarding, SSM documents) and an ephemeral lab with a TTL reaper.
- AWS foundation: organization units, member accounts, budgets with an SCP kill switch, organization CloudTrail.
- Multi-account onboarding: service-managed StackSet deploys the spoke to every account in the Workloads OU (removed when an account leaves it); single-account mode with hub and spoke in one account (ADR 0006).
- Hardening with three pluggable engines (ADR 0007): native controls (17 Linux and 15 Windows controls with CIS references), Ansible (ansible-lockdown CIS L1 for Amazon Linux 2023, fully pinned supply chain) and DSC (CISDSC for Windows Server 2022 L1). All produce per-CIS-rule evidence (`compliance.json`).
- Drift correction: State Manager association with a per-profile interval (30m–24h or off), enforce or audit mode; findings published as `io.patchgate` events and emailed from the hub.
- `AppSet` catalog kind: application bundles composed per instance with the `Patchgate:AppSets` tag and allow-listed by profiles; `ensure: present | latest | absent`, pinned versions and `prune` (removes only what Patchgate installed). Drift also reconciles apps.
- Configuration store in DynamoDB with change history, rollback and alerts for invalid documents (ADR 0008); the resolver composes plans at run time and refuses invalid configuration.
- `patchgate-cli` to manage the configuration store (list, describe, apply with validation and dry run, copy, delete, history, rollback) and instance app sets; `--config-profile` for multi-account use.
- Changes to `Patchgate:*` tags on running instances re-plan the instance without a restart.
- `patchgate-core` Lambda layer with hash-pinned dependencies (`make layer`).

### Changed

- Profiles declare applications in `apps` (replaces the `chocolatey` field) and the app sets they accept in `allowedAppSets`.
- The compiled `plans.json` is replaced by `seed.json` (`make seed`), which seeds the configuration table.
- The spoke StackSet template is delivered from a versioned S3 artifacts bucket (it exceeds the 51 KB inline limit).

### Fixed

- Windows: CloudWatch agent config written without BOM; installer logging no longer fails when a log shipper holds the file open (both found in the first lab run).
- Linux: SSH controls neutralise conflicting directives in other `sshd_config.d` drop-ins.
- Windows: DSC raises the local WinRM `MaxEnvelopeSize` for large MOFs; CIS 2.3.1.1 (disable the only local administrator) is excluded on EC2.
- Windows: MSI, EXE and Chocolatey installs wait and retry while Windows Installer is busy (exit code 1618).
- Concurrent workflow runs for the same instance (launch and tag-change events) are serialised by a per-instance DynamoDB lock.

### Project setup

- Project structure split into cloud-agnostic `core/` and `providers/`.
- Architecture, roadmap, provider contract and threat model documentation.
- ADRs 0001–0008 covering the core/providers split, multi-account model, orchestration, bootstrap, agent catalog, StackSet onboarding, hardening engines and the configuration store.
- Community files: README, AGENTS.md, SECURITY, CONTRIBUTING, Code of Conduct, issue and PR templates.
