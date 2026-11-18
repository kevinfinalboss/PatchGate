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

## Phase 3 — Hardening and drift ✅

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

### 3c — DSC engine (Windows) ✅
- [x] `engine: dsc` baselines: CISDSC 4.0.1 (CIS Windows Server 2022 L1) with AuditPolicyDsc/SecurityPolicyDsc pinned by sha256
- [x] Safe .nupkg extraction, LCM in ApplyOnly (Patchgate drives drift), per-CIS-rule evidence from Test-DscConfiguration before/after, reboot signalling
- [x] Verified in the lab: CIS Windows Server 2022 L1 applied in ~2.5 min; re-run reports 293/293 rules compliant. Found and fixed on real Windows: rules the module cannot skip, WinRM MaxEnvelopeSize for large MOFs, CIS 2.3.1.1 (disable the only admin) excluded on EC2

## Phase 4 — Change management: apps, rollouts and OS patching

Application updates, hardening changes and OS patches are the same problem: changes rolled out in
waves with approval. Phase 4 builds that once and reuses it for everything.

### 4a — Application lifecycle and configuration store ✅
- [x] `AppSet` catalog kind: reusable bundles of apps (Chocolatey on Windows, dnf/apt on Linux)
- [x] Instances compose a profile with app sets via the `Patchgate:AppSets` tag; profiles allow-list which sets they accept
- [x] `ensure: present | latest | absent`, pinned versions, `prune` (removes only what Patchgate installed)
- [x] Drift reconciles apps: reinstall, fix versions, uninstall
- [x] Tag changes on running instances trigger re-planning (no restart needed)
- [x] Configuration in DynamoDB with history, rollback and invalid-config alerts (ADR 0008)
- [x] `patchgate-cli` (Instance Scheduler style): validated writes, copy/delete/history/rollback, instance app sets
- [x] Verified in the lab: app sets at launch (.NET 8 on Windows, git/jq on Linux); `patchgate-cli` tag changes applied without restart (Node.js added; .NET pruned while profile apps stayed; on Linux git pruned, pre-installed jq untouched); app removed by hand reinstalled by drift with email; disallowed app set refused with email; duplicate launch events serialised by the per-instance lock (one install)

### 4b — Drift control ✅
- [x] Drift mode per category (`agents`, `apps`, `hardening`); the profile's `mode` stays the default
- [x] Maintenance window per profile: outside it drift only reports; inside it corrects (at least one correcting run per window). Bootstrap and tag changes stay immediate (ADR 0009)
- [x] `patchgate-cli instance reconcile`: run the drift check now (ignores interval and window)
- [x] Verified in the lab: apps reinstalled while hardening was only reported (mixed modes); outside the window drift only reported (7-Zip removed, LLMNR re-enabled) and `reconcile` corrected both; with a 6h interval the first run inside the window corrected and the next was not due; emails show mode, trigger and window. Found and fixed: SSM returns empty output for multi-step documents unless the step is named

### 4c — Approval engine and rings
One approval engine for every change, built once and used by profile and app set changes and OS patches (ADR 0010).

#### 4c-1 — Engine and rollout of profile and app set changes
- [x] `RolloutPolicy` and `ApproverGroup` kinds; ring, approver group, release and quorum rules as pure functions in `core/lib`
- [x] Signed, expiring approval tokens (confirmation page decides, not the link)
- [x] Accounts registered with their ring and approver groups (`patchgate-cli account`); per-instance override with `Patchgate:Approvers`, limited to the groups the account allows
- [x] Profile and app set changes stay pending per ring and approver group; the resolver serves the released revision
- [x] Rollout workflow: ring by ring, re-plan through the bootstrap workflow, health check, soak, automatic revert on failure
- [x] Email approval per approver group (SES), single use, every decision recorded as audit evidence
- [x] `patchgate-cli rollout list | status | cancel`
- [ ] Verified in the lab

#### 4c-2 — OS patching
- [ ] Patch baselines per OS and patch policy schema; Patch Manager scan/install driven by the engine
- [ ] Pre-patch snapshot, post-patch health checks, automatic rollback (replace root volume)
- [ ] Demo: full approval flow end to end, including a forced rollback

### After Phase 4 — Presentation
Stop adding features and make the project easy to understand in two minutes.
- [ ] Architecture diagram in the README
- [ ] Demo GIF or short video: instance launches → configures itself → someone changes it → drift reverts it → patch with email approval
- [ ] "Decisions and lessons learned": bugs only the real lab revealed (BOM, MSI 1618, duplicate launch events, Lambda layer dependencies)

## Phase 5 — Golden images

- [ ] EC2 Image Builder pipeline applying hardening and agents
- [ ] Inspector scan as a publish gate
- [ ] AMI sharing with the Workloads OU

## Phase 6 — Compliance and emergency patching

- [ ] Resource Data Sync from all accounts to the hub bucket + Athena
- [ ] Dashboard: compliance %, MTTP, open CVEs per ring
- [ ] Monthly evidence report
- [ ] Inspector critical CVE → emergency patch workflow

## Cost notes

The lab is **ephemeral**: everything is created at the start of a test session and destroyed at the end (sessions of at most 2 hours). Only the Terraform state bucket, CloudTrail and budgets persist.

| Item | Estimate |
|---|---|
| One 2–3h session (Linux `t3.micro` + Windows `t3.medium`, public IPs, disks) | ≈ US$0.25–0.50 |
| Control plane (Step Functions, Lambda, EventBridge, SNS, StackSets, Organizations) | ≈ US$0 at lab scale |
| Approval engine (HTTP API, SES, DynamoDB on demand) | ≈ US$0 at lab scale (HTTP API US$1 per million requests; SES US$0.10 per 1,000 emails) |
| Typical month (~15 sessions) | ≈ US$5–10 |
| Optional Wazuh manager | ≈ US$1 per session if created only during tests |

Resources that survive `terraform destroy` and must be cleaned by `make janitor`: snapshots and AMIs created by workflows, log groups created by services, detached volumes and IPs. AWS Config and Security Hub are enabled only inside the ephemeral stack (Phase 6) to avoid recording costs on every rebuild. Secrets use SSM Parameter Store `SecureString` and AWS-managed KMS keys (no monthly key cost).
