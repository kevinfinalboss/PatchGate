# Roadmap

Each phase ends with: working code, updated docs, an ADR for its main decision and a short demo (GIF or video).

## Phase 0 — Foundation ✅

- [x] AWS Organization with Management, Hub and two Workload accounts (dev, prod); OUs `Security` and `Workloads`
- [x] AWS Budget (US$15) with actual and forecast alerts in the management account
- [x] Budget Action: above US$25, attach an SCP that denies `ec2:RunInstances` in workload accounts
- [x] SCPs: deny leaving the organization and region restriction (pre-existing from the AWS organization setup; workload region is `us-east-2`)
- [x] Ephemeral lab tooling: `make up`, `make down`, `make janitor` (removes orphaned snapshots, AMIs, volumes, IPs and log groups tagged by Patchgate)
- [x] TTL reaper: scheduled Lambda that terminates lab instances older than their `Patchgate:TTL` (default 2h)
- [x] Repository tooling: `Makefile`, `ruff`, `mypy`, `pytest`, `tflint`, `checkov`, `shellcheck`, `PSScriptAnalyzer`, pre-commit
- [x] GitHub Actions CI (lint, test, IaC scan)
- [ ] OIDC role for `terraform plan` in CI
- [x] Terraform remote state (S3 with native locking)
- [x] Organization CloudTrail (90-day retention)
- [x] Documentation skeleton, ADRs 0001–0005, community files

## Phase 1 — Multi-account onboarding ✅

- [x] Hub module: central event bus and bus policy restricted to the organization
- [x] Spoke resources as Terraform module **and** StackSet (auto-deploy to Workloads OU), generated from the same source
- [x] `Patchgate-Execution` role and EventBridge forwarder
- [x] Single-account mode example
- [x] Verified: moving an account out of the Workloads OU removes its spoke (~1 min); moving it back re-installs it automatically (~2.5 min)

## Phase 2 — Bootstrap and agent catalog ✅

- [x] JSON Schemas for agent and profile (`v1alpha1`) + `validate-catalog` in CI
- [ ] JSON Schema for patch policy (Phase 4)
- [x] Generic installers (Linux bash, Windows PowerShell) with checksum verification
- [x] Chocolatey support (pinned bootstrap package, package versions)
- [ ] Chocolatey custom/internal source
- [x] Agent templates: CloudWatch Agent, ClamAV, Microsoft Defender
- [ ] Agent templates: Wazuh, generic commercial EDR
- [x] Bootstrap workflow (Step Functions + resolve Lambda + SSM)
- [x] Verified in the lab: Linux (`base-linux`) in ~1.5 min and Windows (`web-windows`) end to end, across accounts
- [ ] Demo recording (GIF)

## Phase 3 — Hardening and drift

### 3a — Native controls engine ✅
- [x] `Control` catalog kind; levels `baseline` / `cis-level1`; per-profile exclusions (ADR 0007)
- [x] 17 Linux and 15 Windows controls with CIS references
- [x] Controls executed by both installers with per-control evidence (`compliance.json`)
- [x] Drift: State Manager association every 30 min, per-profile interval (30m–24h/off), enforce or audit mode
- [x] Drift findings published as `io.patchgate` events → hub → email
- [x] Verified in the lab: hardened-linux (17 controls) and hardened-windows (15) applied on bootstrap; manual drift (SSH root login via a conflicting drop-in, ICMP redirects, disabled ClamAV timer, LLMNR, firewall) corrected by the drift association and emailed from the hub

### 3b — Ansible engine (Linux) ✅
- [x] `Baseline` catalog kind; `engine: ansible` with ansible-lockdown AMAZON2023-CIS 1.5.0 (CIS L1)
- [x] Fully pinned supply chain: role and all collections by sha256 (offline, `--no-deps`), ansible-core via `pip --require-hashes`
- [x] Results mapped to per-CIS-rule evidence (same format as the controls engine)
- [x] Spoke template delivered from a versioned S3 artifacts bucket
- [x] Verified in the lab: full CIS L1 applied at bootstrap in ~5.5 min; re-run reports 198/198 rules compliant (idempotent); SSM access preserved

### 3c — DSC engine (Windows)
- [ ] `engine: dsc` with baseline configurations; same evidence format

## Phase 4 — Patching with approval

- [ ] Patch baselines per OS and patch policy schema (rings, windows, approvers)
- [ ] Patch workflow: scan → ring 0 → soak → email approval → ring 1
- [ ] Pre-patch snapshot, post-patch health checks, automatic rollback
- [ ] Chocolatey application updates in the same flow
- [ ] Demo: full approval flow end to end, including a forced rollback

## Phase 5 — Golden images

- [ ] EC2 Image Builder pipeline applying hardening and agents
- [ ] Inspector scan as a publish gate
- [ ] AMI sharing with the Workloads OU

## Phase 6 — Compliance and emergency patching

- [ ] Resource Data Sync from all accounts to the hub bucket + Athena
- [ ] Dashboard: compliance %, MTTP, open CVEs per ring
- [ ] Monthly evidence report
- [ ] Inspector critical CVE → emergency patch workflow

## Phase 7 — Azure via hybrid nodes

- [ ] Register Azure VMs as SSM hybrid nodes
- [ ] Run bootstrap, hardening and patching from the AWS hub unchanged

## Phase 8 — Native Azure provider

- [ ] `providers/azure/` implementing the provider contract
- [ ] Update Manager, Lighthouse, Event Grid, Durable Functions

## Cost notes

The lab is **ephemeral**: everything is created at the start of a test session and destroyed at the end (sessions of at most 2 hours). Only the Terraform state bucket, CloudTrail and budgets persist.

| Item | Estimate |
|---|---|
| One 2–3h session (Linux `t3.micro` + Windows `t3.medium`, public IPs, disks) | ≈ US$0.25–0.50 |
| Control plane (Step Functions, Lambda, EventBridge, SNS, StackSets, Organizations) | ≈ US$0 at lab scale |
| Typical month (~15 sessions) | ≈ US$5–10 |
| Optional Wazuh manager | ≈ US$1 per session if created only during tests |

Resources that survive `terraform destroy` and must be cleaned by `make janitor`: snapshots and AMIs created by workflows, log groups created by services, detached volumes and IPs. AWS Config and Security Hub are enabled only inside the ephemeral stack (Phase 6) to avoid recording costs on every rebuild. Secrets use SSM Parameter Store `SecureString` and AWS-managed KMS keys (no monthly key cost).
