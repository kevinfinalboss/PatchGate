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

- Project structure split into cloud-agnostic `core/` and `providers/`.
- Architecture, roadmap, provider contract and threat model documentation.
- ADRs 0001–0005 covering core/providers split, multi-account model, orchestration, bootstrap and the agent catalog.
- Community files: README, AGENTS.md, SECURITY, CONTRIBUTING, Code of Conduct, issue and PR templates.
