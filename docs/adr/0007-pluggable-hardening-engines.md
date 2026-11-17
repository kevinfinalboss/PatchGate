# 0007. Pluggable hardening engines, starting with a native controls engine

- **Status:** Accepted
- **Date:** 2026-11-17
- **Changes:** the hardening approach sketched in [0005](0005-declarative-agent-catalog.md) and the architecture (Ansible/DSC only)

## Context

The original plan hardened Linux with Ansible (ansible-lockdown) and Windows with PowerShell DSC.
Both are mature, but each needs extra tooling on the VM, takes minutes per run and reports results in
its own format, which makes per-control evidence and cheap drift checks harder. Some users will still
want those tools for full CIS coverage.

## Options considered

1. **Ansible + DSC only.** Full benchmark coverage; heavy, two different result formats, harder to test.
2. **Native controls engine only.** Light and uniform; covers a curated subset of CIS.
3. **Pluggable engines selected per profile**, with the native engine as default.

## Decision

Option 3.

- A new catalog kind, **Control** (`check` exit 0 = compliant, idempotent `remediate`, CIS reference,
  `baseline` or `cis-level1`). Levels are cumulative; profiles can `exclude` controls.
- Profiles select `hardening.engine`: `controls` now; `ansible` (Linux) and `dsc` (Windows) in Phases 3b/3c.
- Controls are compiled into the install plan and executed by the existing in-VM installers, producing
  `compliance.json` with one result per control (compliant / remediated / non-compliant / failed).
- **Drift**: the last applied plan is stored on the VM. A single SSM State Manager association runs
  every 30 minutes; the executor skips the run unless the profile's `drift.interval` (30m–24h or off)
  is due. `enforce` corrects drift, `audit` only reports. Findings are published as `io.patchgate`
  events, forwarded to the hub and emailed.

## Consequences

- Same evidence format on Linux and Windows, ready for compliance reporting (Phase 6).
- Plans now carry control scripts: the largest profile is ~3.3 KB of the 3.8 KB parameter budget, and the
  spoke template is ~45 KB of CloudFormation's 51 KB inline limit. Phase 3b must move the template to S3
  and may need to ship controls separately from the plan.
- SSH controls validate the configuration (`sshd -t`) before reloading so a bad control cannot lock out
  access; controls with reboot impact are excluded from the native engine for now.
